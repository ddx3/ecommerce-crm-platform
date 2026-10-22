from django.urls import path

from . import views


# Public (customer-facing) endpoints — mounted under /api/chat/
public_patterns = [
    path('start/', views.start_conversation, name='chat_start'),
    path('conversations/<int:conversation_id>/messages/', views.conversation_messages, name='chat_messages'),
    path('conversations/<int:conversation_id>/read/', views.mark_read, name='chat_read'),
]

# Admin (staff-only) endpoints — mounted under /admin/chat/
admin_patterns = [
    path('', views.admin_chat_page, name='chat_admin_page'),
    path('api/conversations/', views.admin_conversations_list, name='chat_admin_list'),
    path('api/conversations/<int:conversation_id>/', views.admin_conversation_messages, name='chat_admin_detail'),
    path('api/conversations/<int:conversation_id>/messages/', views.admin_send_message, name='chat_admin_send'),
    path('api/conversations/<int:conversation_id>/messages/<int:message_id>/edit/', views.admin_edit_message, name='chat_admin_msg_edit'),
    path('api/conversations/<int:conversation_id>/messages/<int:message_id>/delete/', views.admin_delete_message, name='chat_admin_msg_delete'),
    path('api/conversations/<int:conversation_id>/assign/', views.admin_assign, name='chat_admin_assign'),
    path('api/conversations/<int:conversation_id>/close/', views.admin_close, name='chat_admin_close'),
    path('api/conversations/<int:conversation_id>/reopen/', views.admin_reopen, name='chat_admin_reopen'),
    path('api/conversations/<int:conversation_id>/delete/', views.admin_delete_conversation, name='chat_admin_delete'),
    path('api/conversations/<int:conversation_id>/read/', views.admin_mark_read, name='chat_admin_read'),
]
