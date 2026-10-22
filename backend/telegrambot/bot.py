"""aiogram bot: receives messages, runs them through the Claude agent, replies.

Run: `python manage.py run_telegram_bot` (long-polling).
Input security is handled by WhitelistMiddleware (only TELEGRAM_ALLOWED_USER_IDS).
"""
from __future__ import annotations

import base64
import logging
import uuid
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from aiogram import Bot, Dispatcher, BaseMiddleware, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message

from .agent import run_agent
from .context import ToolContext

logger = logging.getLogger(__name__)

TELEGRAM_MSG_LIMIT = 4096
GROUP_PREFIX = '/shop'

# Conversation memory: mix in the latest chat messages so that references like
# 'him/this/add more' work. Only fresh ones; old context just confuses
# the agent.
HISTORY_LIMIT = 5
HISTORY_WINDOW_MIN = 15

HELP_TEXT = (
    '🤖 I am the shop manager\'s assistant. Write in plain language or use the command menu.\n\n'
    '➕ Add:\n'
    '• Product — photo + description (name, price, quantity, category). I will process the photo to the '
    'storefront standard (white background 1080×1080). /add_product\n'
    '• Bundle — name + which products it includes. /add_bundle\n'
    '• Blog post — just a link (an article or YouTube). /add_blog\n\n'
    '🔍 Look up: "find product …", "product details …", "list of categories"\n'
    '✏️ Change: "set the stock to 10 for …", "change the price of … to …", "rename …"\n'
    '📦 Orders: "how many new orders", "the last order", "order 12284"\n'
    '📊 Analytics: "how many orders with product … in a week"\n'
    '↩️ "roll back the last one"\n\n'
    'Everything created is a DRAFT first (hidden). I publish separately on your command.'
)


class WhitelistMiddleware(BaseMiddleware):
    """Lets only allowed users through. Strangers are silently ignored
    (important in groups, so the bot does not answer outsiders)."""

    async def __call__(self, handler, event, data):
        user = data.get('event_from_user')
        allowed = settings.TELEGRAM_ALLOWED_USER_IDS
        if user is None or user.id not in allowed:
            uid = getattr(user, 'id', None)
            logger.warning('Telegram: command rejected from user_id=%s (not in the whitelist)', uid)
            await sync_to_async(_log_denied)(event, user)
            return  # do not pass it further
        return await handler(event, data)


def _log_denied(event, user):
    from .models import CommandLog
    try:
        msg = event if isinstance(event, Message) else getattr(event, 'message', None)
        if msg is None:
            return
        CommandLog.objects.create(
            telegram_user_id=getattr(user, 'id', 0) or 0,
            telegram_username=getattr(user, 'username', '') or '',
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text=(msg.text or msg.caption or '')[:2000],
            status='denied',
        )
    except Exception:
        logger.exception('Could not log the denied command')


def _already_handled(chat_id, message_id) -> bool:
    from .models import CommandLog
    return CommandLog.objects.filter(
        chat_id=chat_id, message_id=message_id, status__in=['ok', 'error']
    ).exists()


def _create_log(*, user, chat_id, message_id, text, had_photo) -> int:
    from .models import CommandLog
    log = CommandLog.objects.create(
        telegram_user_id=user.id,
        telegram_username=user.username or '',
        chat_id=chat_id,
        message_id=message_id,
        text=text[:4000],
        had_photo=had_photo,
    )
    return log.id


def _finalize_log(log_id, *, summary, tool_calls, status, error=''):
    from .models import CommandLog
    CommandLog.objects.filter(pk=log_id).update(
        agent_summary=summary[:8000], tool_calls=tool_calls, status=status,
        error=error[:4000],
    )


def _build_history(chat_id, exclude_log_id) -> str:
    """Short context of the latest chat messages (to resolve 'him/this').

    Only successful ones (status='ok'), fresh ones (<= HISTORY_WINDOW_MIN), the last
    HISTORY_LIMIT, so the request does not bloat and does not cling to an old topic."""
    from .models import CommandLog
    since = timezone.now() - timedelta(minutes=HISTORY_WINDOW_MIN)
    qs = (CommandLog.objects
          .filter(chat_id=chat_id, status='ok', created_at__gte=since)
          .exclude(pk=exclude_log_id)
          .order_by('-created_at')[:HISTORY_LIMIT])
    items = list(qs)[::-1]  # chronological: oldest to newest
    if not items:
        return ''
    lines = ['Context of the previous conversation (from oldest to newest, only for '
             'understanding pronouns such as "him/this/add more"):']
    for c in items:
        u = (c.text or '').strip()[:300]
        a = (c.agent_summary or '').strip()[:300]
        if u:
            lines.append(f'[Operator]: {u}')
        if a:
            lines.append(f'[You]: {a}')
    return '\n'.join(lines)


def _save_temp_image(data: bytes) -> str:
    """Saves a photo as a ProductImage with temp_key (it is attached when the product is created).

    Runs it through image_standardize (rembg: cuts out the background, 1080x1080, white background,
    the storefront standard). If the ML step fails, saves the original (does not block creation)."""
    from django.core.files.base import ContentFile
    from catalog.models import ProductImage
    try:
        from catalog.image_standardize import standardize
        processed = standardize(data)
    except Exception:
        logger.warning('image_standardize failed — saving the original', exc_info=True)
        processed = data
    key = uuid.uuid4().hex
    img = ProductImage(temp_key=key, is_main=True)
    img.image.save(f'{key}.jpg', ContentFile(processed), save=True)
    return key


def _should_handle_group(message: Message, bot_id: int, bot_username: str) -> bool:
    """In a group we react only to a reply to the bot / a mention / the /shop prefix."""
    if not settings.TELEGRAM_REQUIRE_MENTION_IN_GROUP:
        return True
    text = (message.text or message.caption or '')
    if text.strip().startswith(GROUP_PREFIX):
        return True
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot_id:
        return True
    if bot_username and f'@{bot_username}' in text:
        return True
    return False


def _clean_prompt(text: str) -> str:
    text = (text or '').strip()
    if text.startswith(GROUP_PREFIX):
        text = text[len(GROUP_PREFIX):].strip()
    return text


async def _handle_message(message: Message, bot: Bot, override_prompt: str | None = None):
    me = await bot.me()
    is_group = message.chat.type in ('group', 'supergroup')
    if is_group and not _should_handle_group(message, me.id, me.username or ''):
        return

    if await sync_to_async(_already_handled)(message.chat.id, message.message_id):
        return

    user = message.from_user
    had_photo = bool(message.photo)
    if override_prompt is not None:  # the menu command builds the prompt itself
        raw_text = prompt = override_prompt
    else:
        raw_text = message.text or message.caption or ''
        prompt = _clean_prompt(raw_text)

    if not prompt and not had_photo:
        return

    # Anti-spam: no more than N requests per hour per chat (protects the subscription from
    # loops/flooding).
    from core.limits import enforce_tg_chat, LimitExceeded
    try:
        await sync_to_async(enforce_tg_chat)(message.chat.id)
    except LimitExceeded as e:
        await message.reply(str(e))
        return

    log_id = await sync_to_async(_create_log)(
        user=user, chat_id=message.chat.id, message_id=message.message_id,
        text=raw_text, had_photo=had_photo,
    )

    status_msg = await message.reply('⏳ Processing…')
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    image_parts = None
    image_temp_key = ''
    try:
        if had_photo:
            buf = await bot.download(message.photo[-1].file_id)
            data = buf.read()
            image_temp_key = await sync_to_async(_save_temp_image)(data)
            image_parts = [(base64.b64encode(data).decode(), 'image/jpeg')]
            if not prompt:
                prompt = 'Process this product photo.'

        ctx = ToolContext(
            command_log_id=log_id,
            telegram_user_id=user.id,
            max_writes=settings.TELEGRAM_MAX_WRITES_PER_COMMAND,
            image_temp_key=image_temp_key,
        )
        history = await sync_to_async(_build_history)(message.chat.id, log_id)
        if history:
            agent_prompt = f'{history}\n\n---\n\nCurrent operator command: {prompt}'
        else:
            agent_prompt = prompt
        summary, tool_calls = await run_agent(ctx, agent_prompt, image_parts)
        summary = summary or '✅ Done'
        await sync_to_async(_finalize_log)(
            log_id, summary=summary, tool_calls=tool_calls, status='ok')
        await status_msg.edit_text(summary[:TELEGRAM_MSG_LIMIT])
    except Exception as e:
        logger.exception('Error while processing the command')
        await sync_to_async(_finalize_log)(
            log_id, summary='', tool_calls=[], status='error', error=str(e))
        try:
            await status_msg.edit_text(f'❌ An error occurred: {e}'[:TELEGRAM_MSG_LIMIT])
        except Exception:
            pass


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(WhitelistMiddleware())

    @dp.message(CommandStart())
    @dp.message(Command('help'))
    async def cmd_help(message: Message, bot: Bot):
        await message.answer(HELP_TEXT)

    @dp.message(Command('add_product'))
    async def cmd_add_product(message: Message, command: CommandObject, bot: Bot):
        arg = (command.args or '').strip()
        if not arg and not message.photo:
            await message.answer('Send a product photo + description (name, price, quantity, '
                                 'category). A link to the description is also possible.')
            return
        ov = (f'Create a draft product: {arg}' if arg
              else 'Create a draft product from this photo and caption.')
        await _handle_message(message, bot, override_prompt=ov)

    @dp.message(Command('add_bundle'))
    async def cmd_add_bundle(message: Message, command: CommandObject, bot: Bot):
        arg = (command.args or '').strip()
        if not arg:
            await message.answer('Describe the bundle: its name and which products it includes with quantities. '
                                 'E.g.: "Rose Protection bundle: Product A 3ml x2, Product B 1.4g x1".')
            return
        await _handle_message(message, bot, override_prompt=(
            f'Create a draft bundle: {arg}. Find each product through find_product '
            f'and add it to the bundle with the specified quantity.'))

    @dp.message(Command('add_blog'))
    async def cmd_add_blog(message: Message, command: CommandObject, bot: Bot):
        arg = (command.args or '').strip()
        if not arg:
            await message.answer('Send a link to an article or a YouTube video — '
                                 'I will create a draft post.')
            return
        await _handle_message(message, bot, override_prompt=(
            f'Create a draft blog post from this link: {arg}'))

    @dp.message(F.text | F.photo)
    async def on_message(message: Message, bot: Bot):
        await _handle_message(message, bot)

    return dp


BOT_COMMANDS = [
    BotCommand(command='add_product', description='Add a product (photo + description)'),
    BotCommand(command='add_bundle', description='Add a bundle'),
    BotCommand(command='add_blog', description='Add a blog post from a link'),
    BotCommand(command='help', description='What I can do'),
]


async def run_polling(token: str):
    bot = Bot(token)
    dp = build_dispatcher()
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logger.warning('set_my_commands failed', exc_info=True)
    logger.info('Telegram bot started (long-polling)')
    await dp.start_polling(bot)
