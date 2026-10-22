import { useEffect, useState, useMemo, useRef } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import {
  ChevronRight, GraduationCap, PlayCircle, FileText, Lock,
  ChevronLeft, Clock, CheckCircle2,
} from 'lucide-react'
import {
  MediaPlayer, MediaProvider, Track, useMediaState,
  type MediaPlayerInstance,
} from '@vidstack/react'
import {
  defaultLayoutIcons, DefaultVideoLayout,
  type DefaultLayoutTranslations,
} from '@vidstack/react/player/layouts/default'
import '@vidstack/react/player/styles/default/theme.css'
import '@vidstack/react/player/styles/default/layouts/video.css'
import { getCourse, getLesson } from '../../api/courses'
import { sanitizeHtml } from '../../utils/sanitize'
import type { CourseDetail, CourseModule, LessonContent, CourseChapter } from '../../types'
import { programItems, isLeafNode, flattenLessons, ownLesson } from '../../utils/courseProgram'
import { lockScroll } from '../../components/ui/scrollLock'

/** Sidebar branch: a node without subsections is a lesson (a link row); with subsections it is a
 * section. A section can also have its own lesson, in which case its title is itself a link to that
 * lesson.
 */
function SidebarNode({
  module: m, num, slug, currentId, onPick, depth = 0,
}: {
  module: CourseModule
  num: string
  slug: string
  currentId: number | null
  onPick: () => void
  depth?: number
}) {
  const row = (l: { id: number; title: string; lesson_type: string; poster_url: string | null },
               n: string, bold = false) => {
    const Icon = l.lesson_type === 'video' ? PlayCircle : FileText
    const isCurrent = currentId === l.id
    return (
      <Link
        to={`/course/${slug}/learn/${l.id}`}
        onClick={onPick}
        className={`flex items-center gap-2.5 px-4 py-3 lg:py-2.5 text-sm transition-colors ${
          isCurrent
            ? 'bg-brand-bg text-brand font-bold border-l-2 border-brand'
            : `${bold ? 'text-gray-800 font-bold' : 'text-gray-700'} hover:bg-gray-50 border-l-2 border-transparent`
        }`}
        style={depth ? { paddingLeft: 16 + depth * 12 } : undefined}
      >
        <span className="text-xs text-gray-400 w-8 shrink-0 tabular-nums">{n}.</span>
        {l.poster_url ? (
          <img src={l.poster_url} alt="" className="w-9 h-12 object-cover rounded shrink-0 bg-gray-100" />
        ) : (
          <span className="w-9 h-12 flex items-center justify-center rounded bg-gray-100 shrink-0">
            <Icon size={16} className="text-gray-400" />
          </span>
        )}
        <span className="flex-1 min-w-0 line-clamp-2">{l.title}</span>
      </Link>
    )
  }

  const own = ownLesson(m)
  if (isLeafNode(m) && own) {
    return <ul className="divide-y divide-gray-50"><li>{row(own, num)}</li></ul>
  }

  return (
    <>
      {own ? row(own, num, true) : (
        <div
          className="px-4 py-2.5 flex items-baseline gap-1.5 text-sm font-bold text-gray-800"
          style={depth ? { paddingLeft: 16 + depth * 12 } : undefined}
        >
          <span className="tabular-nums shrink-0">{num}.</span>
          <span>{m.title}</span>
        </div>
      )}
      <ul className="divide-y divide-gray-50">
        {programItems(m, num).map(it => (
          it.kind === 'lesson'
            ? <li key={`l${it.lesson.id}`}>{row(it.lesson, it.num)}</li>
            : (
              <li key={`m${it.module.id}`}>
                <SidebarNode
                  module={it.module}
                  num={it.num}
                  slug={slug}
                  currentId={currentId}
                  onPick={onPick}
                  depth={depth + 1}
                />
              </li>
            )
        ))}
      </ul>
    </>
  )
}

export default function CourseLearnPage() {
  const { slug, lessonId } = useParams<{ slug: string; lessonId?: string }>()
  const navigate = useNavigate()
  const isFull = lessonId === 'full'

  const [course, setCourse] = useState<CourseDetail | null>(null)
  const [lesson, setLesson] = useState<LessonContent | null>(null)
  const [loading, setLoading] = useState(true)
  const [lessonLoading, setLessonLoading] = useState(false)
  const [lessonError, setLessonError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  // Video links are signed and expire. Bumping this counter re-requests the lesson/course and
  // yields fresh URLs; the player triggers it on a media error.
  // Attempts are limited: a player error is not always about an expired token (it can be a 404 for
  // a missing file, a 429, a broken codec), and without a limit the re-request would loop forever:
  // new src -> player remount -> error again.
  const [srcNonce, setSrcNonce] = useState(0)
  const retriesRef = useRef(0)
  useEffect(() => { retriesRef.current = 0 }, [lessonId])
  const refreshVideoSrc = () => {
    if (retriesRef.current >= 2) return
    retriesRef.current += 1
    setSrcNonce(n => n + 1)
  }
  // The course's full video comes with the course itself, so refreshing the course only makes sense
  // in that mode; for a lesson, re-requesting the lesson alone is enough.
  const fullNonce = isFull ? srcNonce : 0

  // Scroll the page to the top when the lesson changes (especially for long text lessons).
  useEffect(() => {
    if (lessonId) window.scrollTo({ top: 0, behavior: 'auto' })
  }, [lessonId])

  // Lock background scroll while the mobile sidebar is open.
  useEffect(() => {
    if (!sidebarOpen) return
    return lockScroll()
  }, [sidebarOpen])

  // Load the course
  useEffect(() => {
    if (!slug) return
    setLoading(true)
    setError(null)
    getCourse(slug)
      .then(setCourse)
      .catch(e => {
        console.error('Course load error:', e)
        setError(e?.response?.status === 404 ? 'Course not found' : 'Loading error')
      })
      .finally(() => setLoading(false))
    // lessonId is NOT needed in the dependencies: the course is heavy (it pulls product cards), and
    // a re-request on every lesson click collapsed the page into a skeleton.
  }, [slug, fullNonce])

  // Without access, only allow entry if a preview lesson is being opened (otherwise clicking
  // "Try for free" would not work).
  useEffect(() => {
    if (!course || course.has_access || !slug) return
    const openId = lessonId ? Number(lessonId) : null
    const openLesson = openId ? flattenLessons(course.modules).find(l => l.id === openId) : null
    if (!openLesson || !openLesson.is_preview) navigate(`/course/${slug}`, { replace: true })
  }, [course, lessonId, slug, navigate])

  // Flat list of all lessons in curriculum order (for prev/next navigation).
  // Lessons can be at any nesting level, so we walk the tree.
  const flatLessons = useMemo(() => {
    if (!course) return []
    return flattenLessons(course.modules)
  }, [course])

  // If lessonId is not given, take the first available lesson
  useEffect(() => {
    if (!course || !slug) return
    if (!lessonId) {
      const first = flatLessons[0]
      // There may be no lessons at all: a course with a single full video. In that case lead to it,
      // otherwise "Go to learning" opened an empty panel.
      if (first) navigate(`/course/${slug}/learn/${first.id}`, { replace: true })
      else if (course.full_video_url) navigate(`/course/${slug}/learn/full`, { replace: true })
    }
  }, [course, lessonId, slug, navigate, flatLessons])

  // Load the lesson content (full video is a separate mode, with no API request)
  useEffect(() => {
    if (!slug || !lessonId || isFull) { setLesson(null); setLessonError(null); return }
    setLessonLoading(true)
    setLessonError(null)
    getLesson(slug, Number(lessonId))
      .then(data => { setLesson(data); setLessonError(null) })
      .catch(e => {
        console.error('Lesson load error:', e)
        const status = e?.response?.status
        if (status === 403) {
          navigate(`/course/${slug}`, { replace: true })
          return
        }
        setLesson(null)
        setLessonError(status === 404 ? 'Lesson not found' : 'Lesson loading error')
      })
      .finally(() => setLessonLoading(false))
  }, [slug, lessonId, isFull, navigate, srcNonce])

  const currentIdx = lesson ? flatLessons.findIndex(l => l.id === lesson.id) : -1
  const prevLesson = currentIdx > 0 ? flatLessons[currentIdx - 1] : null
  const nextLesson = currentIdx >= 0 && currentIdx < flatLessons.length - 1 ? flatLessons[currentIdx + 1] : null
  const firstLesson = flatLessons[0] ?? null

  if (loading) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 py-8">
        <div className="animate-pulse h-96 bg-gray-100 rounded-2xl" />
      </div>
    )
  }

  if (error || !course) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 py-20 text-center">
        <div className="w-20 h-20 rounded-full bg-brand-bg text-brand flex items-center justify-center mx-auto mb-4"><GraduationCap size={36} /></div>
        <p className="text-xl font-bold text-gray-700">{error || 'Course not found'}</p>
        <Link to="/courses" className="inline-flex items-center gap-1 mt-4 text-brand font-bold hover:underline">
          <ChevronLeft size={18} /> Back to courses
        </Link>
      </div>
    )
  }

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-5 sm:py-7">
      <nav className="flex items-center gap-2 text-xs sm:text-sm text-gray-400 mb-4">
        <Link to="/" className="hover:text-brand">Home</Link>
        <ChevronRight size={14} />
        <Link to="/courses" className="hover:text-brand">Courses</Link>
        <ChevronRight size={14} />
        <Link to={`/course/${course.slug}`} className="hover:text-brand line-clamp-1">{course.name}</Link>
      </nav>

      <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-4 lg:gap-6">
        {/* ── Mobile overlay for the sidebar ── */}
        {sidebarOpen && (
          <div
            className="lg:hidden fixed inset-0 bg-black/40 z-[100]"
            onClick={() => setSidebarOpen(false)}
            aria-hidden="true"
          />
        )}
        {/* ── Sidebar with the program ── */}
        <aside
          id="course-sidebar"
          aria-label="Course program"
          className={`${sidebarOpen ? 'fixed inset-y-0 left-0 w-[85vw] max-w-[360px] z-[110] overflow-y-auto' : 'hidden'} lg:relative lg:block lg:w-auto lg:inset-auto lg:z-auto bg-white border border-gray-200 rounded-2xl overflow-hidden lg:sticky lg:top-[calc(var(--header-h,80px)+1rem)] lg:self-start lg:max-h-[calc(100vh-var(--header-h,80px)-2rem)] lg:overflow-y-auto`}
        >
          <div className="relative px-4 py-2.5 border-b border-gray-100 flex items-center justify-start sticky top-0 bg-white z-10">
            <Link to={`/course/${course.slug}`} className="text-sm text-brand font-bold hover:underline inline-flex items-center gap-1 -ml-1"><ChevronLeft size={16} className="shrink-0" /> Back to course overview</Link>
            <button
              onClick={() => setSidebarOpen(false)}
              className="lg:hidden absolute right-2 top-1/2 -translate-y-1/2 w-8 h-8 flex items-center justify-center text-gray-400"
              aria-label="Close program"
            >
              <ChevronLeft size={18} />
            </button>
          </div>
          {course.full_video_url && (
            <div className="border-b border-gray-100">
              <Link
                to={`/course/${course.slug}/learn/full`}
                onClick={() => setSidebarOpen(false)}
                className={`flex items-center gap-2.5 px-4 py-3 text-sm transition-colors ${
                  isFull
                    ? 'bg-brand-bg text-brand font-bold border-l-2 border-brand'
                    : 'text-gray-700 hover:bg-gray-50 border-l-2 border-transparent'
                }`}
              >
                {course.full_video_poster_url ? (
                  <img
                    src={course.full_video_poster_url}
                    alt=""
                    className="w-16 h-9 object-cover rounded shrink-0 bg-gray-100"
                  />
                ) : (
                  <span className="w-16 h-9 flex items-center justify-center rounded bg-gray-100 shrink-0">
                    <PlayCircle size={16} className="text-gray-400" />
                  </span>
                )}
                <span className="flex-1 min-w-0">
                  <span className="block font-bold">Full version</span>
                  <span className="block text-xs text-gray-400 font-normal">The whole course in one video</span>
                </span>
              </Link>
            </div>
          )}
          {course.modules.map((m, mIdx) => (
            <div key={m.id} className="border-b border-gray-100 last:border-b-0">
              <SidebarNode
                module={m}
                num={String(mIdx + 1)}
                slug={course.slug}
                currentId={lesson?.id ?? null}
                onPick={() => setSidebarOpen(false)}
              />
            </div>
          ))}
        </aside>

        {/* ── Main area ── */}
        <div>
          <button
            onClick={() => setSidebarOpen(true)}
            aria-expanded={sidebarOpen}
            aria-controls="course-sidebar"
            className="lg:hidden mb-3 inline-flex items-center gap-2 text-sm text-brand font-bold"
          >
            <ChevronRight size={16} /> Course program
          </button>

          {isFull ? (
            course.full_video_url ? (
              <>
                <h1 className="text-xl sm:text-2xl font-bold text-gray-900 mb-5">Full course video</h1>
                <VideoFilePlayer
                  src={course.full_video_url}
                  poster={course.full_video_poster_url}
                  chapters={course.full_video_chapters}
                  title="Full course video"
                  onSrcExpired={refreshVideoSrc}
                />
                {firstLesson && (
                  <div className="flex items-center justify-end mt-6">
                    <Link
                      to={`/course/${course.slug}/learn/${firstLesson.id}`}
                      className="flex items-center gap-2 px-4 py-2.5 bg-brand hover:bg-brand-hover text-white rounded-xl text-sm leading-none transition-colors sm:max-w-[60%]"
                    >
                      <span className="line-clamp-1">Go to lessons: {firstLesson.title}</span>
                      <ChevronRight size={16} className="shrink-0" />
                    </Link>
                  </div>
                )}
              </>
            ) : (
              <div className="text-center py-16">
                <Lock size={40} className="mx-auto mb-3 text-gray-200" />
                <p className="text-base font-bold text-gray-700">The full video is not available</p>
              </div>
            )
          ) : lessonLoading ? (
            <div className="animate-pulse">
              <div className="aspect-video bg-gray-100 rounded-2xl mb-4" />
              <div className="h-6 w-2/3 bg-gray-100 rounded mb-2" />
              <div className="h-4 w-1/2 bg-gray-100 rounded" />
            </div>
          ) : lessonError ? (
            <div className="text-center py-16">
              <Lock size={40} className="mx-auto mb-3 text-gray-200" />
              <p className="text-base font-bold text-gray-700 mb-2">{lessonError}</p>
              <Link to={`/course/${course.slug}`} className="inline-flex items-center gap-1 mt-2 text-brand font-bold hover:underline">
                <ChevronLeft size={18} /> Back to course overview
              </Link>
            </div>
          ) : lesson ? (
            <>
              <h1 className="text-xl sm:text-2xl font-bold text-gray-900 mb-5">{lesson.title}</h1>

              {lesson.lesson_type === 'video' ? (
                <>
                  <LessonVideo lesson={lesson} onSrcExpired={refreshVideoSrc} />
                  {lesson.content && (
                    <div
                      className="prose prose-sm sm:prose-base max-w-none text-gray-800 bg-white border border-gray-200 rounded-2xl p-5 sm:p-7 mt-4"
                      dangerouslySetInnerHTML={{ __html: sanitizeHtml(lesson.content) }}
                    />
                  )}
                </>
              ) : (
                <div
                  className="prose prose-sm sm:prose-base max-w-none text-gray-800 bg-white border border-gray-200 rounded-2xl p-5 sm:p-7"
                  dangerouslySetInnerHTML={{
                    __html: lesson.content
                      ? sanitizeHtml(lesson.content)
                      : '<p class="text-gray-400">Content coming soon.</p>',
                  }}
                />
              )}

              {/* Navigation prev/next */}
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mt-6">
                {prevLesson ? (
                  <Link
                    to={`/course/${course.slug}/learn/${prevLesson.id}`}
                    className="flex items-center gap-2 px-4 py-2.5 border border-gray-200 hover:border-brand hover:text-brand text-gray-700 rounded-xl text-sm leading-none transition-colors sm:max-w-[45%]"
                  >
                    <ChevronLeft size={16} className="shrink-0" />
                    <span className="line-clamp-1">{prevLesson.title}</span>
                  </Link>
                ) : <span />}
                {nextLesson ? (
                  <Link
                    to={`/course/${course.slug}/learn/${nextLesson.id}`}
                    className="flex items-center gap-2 px-4 py-2.5 bg-brand hover:bg-brand-hover text-white rounded-xl text-sm leading-none transition-colors sm:max-w-[45%]"
                  >
                    <span className="line-clamp-1">{nextLesson.title}</span>
                    <ChevronRight size={16} className="shrink-0" />
                  </Link>
                ) : (
                  <Link
                    to={`/course/${course.slug}`}
                    className="flex items-center gap-2 px-4 py-2.5 bg-gray-100 hover:bg-brand-bg hover:text-brand text-gray-600 rounded-xl text-sm leading-none transition-colors"
                  >
                    <CheckCircle2 size={16} className="shrink-0" /> Course completed
                  </Link>
                )}
              </div>
            </>
          ) : (
            <div className="text-center py-16 text-gray-400">
              <Lock size={40} className="mx-auto mb-3 text-gray-200" />
              <p>Choose a lesson from the list</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// English UI texts for the Vidstack player (custom wording over the library defaults).
const PLAYER_EN: Partial<DefaultLayoutTranslations> = {
  'Caption Styles': 'Caption styles',
  'Captions look like this': 'Captions look like this',
  'Closed-Captions Off': 'Captions off',
  'Closed-Captions On': 'Captions on',
  'Display Background': 'Display background',
  'Enter Fullscreen': 'Enter fullscreen',
  'Enter PiP': 'Picture in picture',
  'Exit Fullscreen': 'Exit fullscreen',
  'Exit PiP': 'Exit picture in picture',
  'Google Cast': 'Cast',
  'Keyboard Animations': 'Keyboard animations',
  'Seek Backward': 'Back',
  'Seek Forward': 'Forward',
  'Skip To Live': 'Skip to live',
  'Text Background': 'Text background',
  Accessibility: 'Accessibility',
  AirPlay: 'AirPlay',
  Announcements: 'Announcements',
  Audio: 'Audio',
  Auto: 'Auto',
  Boost: 'Boost',
  Captions: 'Captions',
  Chapters: 'Chapters',
  Color: 'Color',
  Connected: 'Connected',
  Connecting: 'Connecting…',
  Continue: 'Continue',
  Default: 'Default',
  Disabled: 'Disabled',
  Disconnected: 'Disconnected',
  Download: 'Download',
  Family: 'Font',
  Font: 'Font',
  Fullscreen: 'Fullscreen',
  LIVE: 'LIVE',
  Loop: 'Loop',
  Mute: 'Mute',
  Normal: 'Normal',
  Off: 'Off',
  Opacity: 'Opacity',
  Pause: 'Pause',
  PiP: 'Picture in picture',
  Play: 'Play',
  Playback: 'Playback',
  Quality: 'Quality',
  Replay: 'Replay',
  Reset: 'Reset',
  Seek: 'Seek',
  Settings: 'Settings',
  Shadow: 'Shadow',
  Size: 'Size',
  Speed: 'Speed',
  Text: 'Text',
  Track: 'Track',
  Unmute: 'Unmute',
  Volume: 'Volume',
}

// Vidstack player: custom controls + chapters on the progress bar (like YouTube).
// Optionally, below the player there is a chapter list with seeking.
function VideoFilePlayer({
  src, poster, chapters, title, onSrcExpired,
}: {
  src: string
  poster?: string | null
  chapters?: CourseChapter[]
  title?: string
  onSrcExpired?: () => void
}) {
  const playerRef = useRef<MediaPlayerInstance>(null)

  // Pass chapters to the player as a chapters track via a blob URL with WebVTT.
  const chaptersUrl = useMemo(() => {
    if (!chapters || chapters.length === 0) return null
    const vtt = buildChaptersVtt(chapters)
    return URL.createObjectURL(new Blob([vtt], { type: 'text/vtt' }))
  }, [chapters])

  useEffect(() => {
    return () => { if (chaptersUrl) URL.revokeObjectURL(chaptersUrl) }
  }, [chaptersUrl])

  return (
    <>
      <div className="flex justify-center bg-black rounded-2xl overflow-hidden">
        <MediaPlayer
          ref={playerRef}
          key={src}
          title={title}
          src={src}
          poster={poster ?? undefined}
          playsInline
          aspectRatio="9/16"
          // The video link is signed and lives for a limited time. If the tab sat idle longer, the
          // source returns 410 and the player silently breaks.
          // We ask the parent to re-request the lesson and get a fresh link.
          onError={() => onSrcExpired?.()}
          className="max-w-full h-[var(--cl-vh)] sm:h-[var(--cl-vh-sm)]"
          style={{
            width: 'auto',
            ['--cl-vh' as string]: 'min(62vh, 760px)',
            ['--cl-vh-sm' as string]: 'min(78vh, 760px)',
          }}
        >
          <MediaProvider>
            {chaptersUrl && (
              <Track src={chaptersUrl} kind="chapters" lang="uk" default />
            )}
          </MediaProvider>
          <DefaultVideoLayout icons={defaultLayoutIcons} translations={PLAYER_EN} />
        </MediaPlayer>
      </div>
      {chapters && chapters.length > 0 && (
        <ChapterList chapters={chapters} playerRef={playerRef} />
      )}
    </>
  )
}

// Build WebVTT from the chapters for the player's chapters track.
function buildChaptersVtt(chapters: CourseChapter[]): string {
  const out = ['WEBVTT', '']
  chapters.forEach((ch, i) => {
    const start = ch.time
    const end = i < chapters.length - 1 ? chapters[i + 1].time : start + 600
    out.push(`${vttTime(start)} --> ${vttTime(end)}`)
    out.push(ch.title)
    out.push('')
  })
  return out.join('\n')
}

function vttTime(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(h)}:${p(m)}:${p(s)}.000`
}

// Chapter list below the player: click seeks the video, the active chapter is highlighted.
function ChapterList({
  chapters, playerRef,
}: {
  chapters: CourseChapter[]
  playerRef: React.RefObject<MediaPlayerInstance | null>
}) {
  const currentTime = useMediaState('currentTime', playerRef)

  let activeIdx = 0
  for (let i = 0; i < chapters.length; i++) {
    if (currentTime >= chapters[i].time) activeIdx = i
    else break
  }

  const seekTo = (time: number) => {
    const p = playerRef.current
    if (!p) return
    p.currentTime = time
    p.play().catch(() => {})
  }

  return (
    <div className="mt-4 bg-white border border-gray-200 rounded-2xl overflow-hidden">
      <div className="px-4 py-2.5 bg-gray-50 flex items-center gap-2 text-sm font-bold text-gray-800 leading-none">
        <Clock size={16} className="shrink-0" /> Timecodes
      </div>
      <ul className="divide-y divide-gray-50 max-h-[420px] overflow-y-auto">
        {chapters.map((ch, i) => (
          <li key={i}>
            <button
              onClick={() => seekTo(ch.time)}
              className={`w-full flex items-center gap-3 px-4 py-2.5 text-left text-sm transition-colors ${
                i === activeIdx
                  ? 'bg-brand-bg text-brand font-bold border-l-2 border-brand'
                  : 'text-gray-700 hover:bg-gray-50 border-l-2 border-transparent'
              }`}
            >
              <span className="font-bold tabular-nums w-12 shrink-0 text-brand">
                {formatTimecode(ch.time)}
              </span>
              <span className="flex-1 min-w-0">{ch.title}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function formatTimecode(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

// Lesson player: own <video> for an uploaded file, otherwise an embed of an external video.
function LessonVideo({ lesson, onSrcExpired }: { lesson: LessonContent; onSrcExpired?: () => void }) {
  if (lesson.video_file_url) {
    return (
      <VideoFilePlayer
        src={lesson.video_file_url}
        poster={lesson.poster_url}
        title={lesson.title}
        onSrcExpired={onSrcExpired}
      />
    )
  }
  return (
    <VideoEmbed
      url={lesson.video_url}
      title={lesson.title}
      start={lesson.video_start}
      end={lesson.video_end}
    />
  )
}

function VideoEmbed({
  url, title, start, end,
}: {
  url: string
  title: string
  start?: number | null
  end?: number | null
}) {
  const embed = useMemo(() => toEmbedUrl(url, start, end), [url, start, end])
  if (!embed) {
    return (
      <div className="aspect-video bg-gray-50 rounded-2xl flex flex-col items-center justify-center text-gray-500 text-sm gap-3 p-4">
        <span>Could not recognize the video link</span>
        {url && (
          <a href={url} target="_blank" rel="noopener noreferrer" className="text-brand font-bold underline">
            Open in a new tab
          </a>
        )}
      </div>
    )
  }
  return (
    <div className="aspect-video rounded-2xl overflow-hidden bg-black">
      <iframe
        src={embed}
        title={title}
        className="w-full h-full"
        frameBorder={0}
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
        allowFullScreen
      />
    </div>
  )
}

function toEmbedUrl(url: string, start?: number | null, end?: number | null): string | null {
  if (!url) return null
  // YouTube: youtu.be/ID, youtube.com/watch?v=ID, youtube.com/embed/ID
  const ytMatch = url.match(/(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|embed\/|shorts\/))([\w-]{6,})/)
  if (ytMatch) {
    const params = new URLSearchParams()
    if (start && start > 0) params.set('start', String(start))
    if (end && end > 0) params.set('end', String(end))
    const qs = params.toString()
    return `https://www.youtube.com/embed/${ytMatch[1]}${qs ? `?${qs}` : ''}`
  }
  // Vimeo: vimeo.com/ID, player.vimeo.com/video/ID (supports only a start time via #t)
  const vmMatch = url.match(/(?:vimeo\.com\/(?:video\/)?|player\.vimeo\.com\/video\/)(\d+)/)
  if (vmMatch) {
    const base = `https://player.vimeo.com/video/${vmMatch[1]}`
    return start && start > 0 ? `${base}#t=${start}s` : base
  }
  return null
}
