from django.contrib import admin
from django.urls import path, include, re_path
from django.shortcuts import redirect
from django.contrib.sitemaps.views import sitemap as sitemap_view, index as sitemap_index
from django.views.decorators.cache import cache_page
from core.sitemaps import SITEMAPS
from core.robots import robots_txt
from core.shell import shell_view
from orders.views import nova_poshta_proxy, nova_poshta_public_proxy, create_order, monobank_webhook, monobank_personal_webhook, process_invoice, confirm_invoice, revert_invoice, search_variants, search_bundles, bundle_items, generate_docx, export_file, update_order_field, my_orders, pay_order, upload_payment_screenshot, mark_order_paid, request_payment_details, order_public_status, parse_order, order_item_components, order_item_instruction, change_payment_method, parse_writeoff, confirm_writeoff, pending_stock_returns, confirm_stock_returns, admin_order_payment_link
from orders.admin_pdf import order_receipt_pdf
from orders.models import Order
from analyst import views as analyst_views
from catalog.api_views import categories_list, categories_flat, products_list, product_facets, product_detail, bought_together, bundles_list, bundle_detail, search, site_reviews, reply_to_review, product_reviews, bundle_reviews, notify_stock, brands_list, maintenance_status, site_settings, favorite_set, favorite_sync, flora_catalog_batch, flora_catalog_delta
from courses.api_views import courses_list, course_detail, lesson_content, my_courses, product_courses, course_video
from courses.views import (save_course_field, admin_course_video,
                           course_related_search, course_related_details)
from users.views import register, login, me, logout, update_profile, search_users, password_reset_request, password_reset_confirm, finalize_guest
from chat.urls import public_patterns as chat_public_patterns, admin_patterns as chat_admin_patterns
from blog.views import fetch_youtube_info
from core.antibot import antibot_token
from rest_framework_simplejwt.views import TokenRefreshView
from django.conf import settings
from django.conf.urls.static import static
from catalog.views import (
    run_parse_dropshipping,
    notifications_view, notifications_count, notification_review_action, notification_question_action, notification_stock_action, notification_payment_action,
    notification_dropsync_action, notification_set_step, notification_requisites_action,

    bundles_data_view,
    save_variant_field, save_bundle_field, save_brand_field, save_category_field,
    variant_fresh_demand,
    get_variant_price_stock,
    variant_search, variant_details, bundle_details,
    generate_sku, check_sku,
    upload_product_photo, delete_product_photo,
    replace_product_photo, process_product_photo, apply_product_photo_std, reorder_product_photos, get_temp_photos, cleanup_temp_photos,
    get_variant_photos, get_product_photos, set_photo_variant, set_photo_main, set_photo_assignment,
    get_characteristics, save_characteristic, delete_characteristic, reorder_characteristics, set_characteristic_assignment,
    get_composition, save_composition_item, delete_composition_item, reorder_composition,
    reorder_categories,
    upload_bundle_photo, get_bundle_photos, delete_bundle_photo,
    replace_bundle_photo, reorder_bundle_photos, get_bundle_temp_photos, toggle_bundle_photo_hidden,
    revert_history,
    upload_brand_photo, get_brand_photos, delete_brand_photo, replace_brand_photo, reorder_brand_photos,
    generate_description,
    normalize_bundle_instruction, bundle_instruction_docx,
    bulk_sources_list, bulk_source_save, bulk_source_delete, bulk_source_link_variant,
    goto_paired_product,
)

urlpatterns = [
    path('admin/users/search/', search_users),
    # Legacy /admin/notifications/ -> redirect to the first section (this used to be a combined page
    # with tabs).
    path('admin/notifications/', lambda request: redirect('/admin/notifications/payments/')),
    path('admin/notifications/count/', notifications_count),
    path('admin/notifications/review-action/', notification_review_action),
    path('admin/notifications/question-action/', notification_question_action),
    path('admin/notifications/stock-action/', notification_stock_action),
    path('admin/notifications/payment-action/', notification_payment_action),
    path('admin/notifications/dropsync-action/', notification_dropsync_action),
    path('admin/notifications/requisites-action/', notification_requisites_action),
    path('admin/notifications/set-step/', notification_set_step),
    # Analyst (daily analyst): its own page in the "Inbox" group, not a section of
    # notifications.html.
    path('admin/analyst/', admin.site.admin_view(analyst_views.inbox_view), name='analyst_inbox'),
    path('admin/analyst/action/', admin.site.admin_view(analyst_views.action_view), name='analyst_action'),
    path('admin/analyst/demo/', admin.site.admin_view(analyst_views.demo_view), name='analyst_demo'),
    # Fixes: plan (writes nothing) → apply (after "Apply") → revert.
    path('admin/analyst/fix/plan/', admin.site.admin_view(analyst_views.plan_view), name='analyst_fix_plan'),
    path('admin/analyst/fix/plan-all/', admin.site.admin_view(analyst_views.plan_all_view), name='analyst_fix_plan_all'),
    path('admin/analyst/fix/apply/', admin.site.admin_view(analyst_views.apply_view), name='analyst_fix_apply'),
    path('admin/analyst/fix/revert/', admin.site.admin_view(analyst_views.revert_view), name='analyst_fix_revert'),
    # Separate pages for each section: no tabs, one table per page.
    path('admin/notifications/<str:section>/', notifications_view, name='notifications_section'),

    path('admin/catalog/bundles-data/', bundles_data_view),
    path('admin/catalog/instruction-normalize/', normalize_bundle_instruction),
    path('admin/catalog/bundle/<int:bundle_id>/instruction-docx/', bundle_instruction_docx),
    path('admin/catalog/generate-sku/', generate_sku),
    path('admin/catalog/check-sku/', check_sku),
    path('admin/catalog/upload-photo/', upload_product_photo),
    path('admin/catalog/delete-photo/', delete_product_photo),
    path('admin/catalog/replace-photo/', replace_product_photo),
    path('admin/catalog/process-photo/', process_product_photo),
    path('admin/catalog/apply-photo-std/', apply_product_photo_std),
    path('admin/catalog/reorder-photos/', reorder_product_photos),
    path('admin/catalog/temp-photos/', get_temp_photos),
    path('admin/catalog/cleanup-temp-photos/', cleanup_temp_photos),
    path('admin/catalog/variant-photos/', get_variant_photos),
    path('admin/catalog/product-photos/', get_product_photos),
    path('admin/catalog/set-photo-variant/', set_photo_variant),
    path('admin/catalog/set-photo-main/', set_photo_main),
    path('admin/catalog/set-photo-assignment/', set_photo_assignment),
    path('admin/catalog/characteristics/', get_characteristics),
    path('admin/catalog/characteristics/save/', save_characteristic),
    path('admin/catalog/characteristics/delete/', delete_characteristic),
    path('admin/catalog/characteristics/reorder/', reorder_characteristics),
    path('admin/catalog/characteristics/set-assignment/', set_characteristic_assignment),
    path('admin/catalog/composition/', get_composition),
    path('admin/catalog/composition/save/', save_composition_item),
    path('admin/catalog/composition/delete/', delete_composition_item),
    path('admin/catalog/composition/reorder/', reorder_composition),
    path('admin/catalog/bulk-sources/', bulk_sources_list),
    path('admin/catalog/bulk-sources/save/', bulk_source_save),
    path('admin/catalog/bulk-sources/delete/', bulk_source_delete),
    path('admin/catalog/bulk-sources/link-variant/', bulk_source_link_variant),
    path('admin/catalog/product/<int:product_id>/goto-paired/', goto_paired_product),
    path('admin/catalog/reorder-categories/', reorder_categories),
    path('admin/catalog/variant-price-stock/', get_variant_price_stock),
    path('admin/catalog/variant-search/', variant_search),
    path('admin/catalog/variant-details/', variant_details),
    path('admin/catalog/bundle-details/', bundle_details),
    path('admin/catalog/save-variant-field/', save_variant_field),
    path('admin/catalog/variant-fresh-demand/', variant_fresh_demand),
    path('admin/catalog/save-bundle-field/', save_bundle_field),
    path('admin/catalog/save-brand-field/', save_brand_field),
    path('admin/catalog/save-category-field/', save_category_field),
    path('admin/courses/save-course-field/', save_course_field),
    path('admin/courses/video/', admin_course_video),
    path('admin/courses/related-search/', course_related_search),
    path('admin/courses/related-details/', course_related_details),
    path('admin/catalog/upload-bundle-photo/', upload_bundle_photo),
    path('admin/catalog/bundle-photos/', get_bundle_photos),
    path('admin/catalog/delete-bundle-photo/', delete_bundle_photo),
    path('admin/catalog/replace-bundle-photo/', replace_bundle_photo),
    path('admin/catalog/reorder-bundle-photos/', reorder_bundle_photos),
    path('admin/catalog/toggle-bundle-photo-hidden/', toggle_bundle_photo_hidden),
    path('admin/catalog/bundle-temp-photos/', get_bundle_temp_photos),
    path('admin/catalog/upload-brand-photo/', upload_brand_photo),
    path('admin/catalog/brand-photos/', get_brand_photos),
    path('admin/catalog/delete-brand-photo/', delete_brand_photo),
    path('admin/catalog/replace-brand-photo/', replace_brand_photo),
    path('admin/catalog/reorder-brand-photos/', reorder_brand_photos),
    path('admin/catalog/generate-description/', generate_description),
    path('admin/catalog/dropshipping/import/', run_parse_dropshipping),
    path('admin/history/revert/<int:log_id>/', revert_history),
    path('admin/orders/item-components/', order_item_components),
    path('admin/orders/nova-poshta/', nova_poshta_proxy),
    path('admin/orders/parse-order/', parse_order),
    path('admin/orders/parse-writeoff/', parse_writeoff),
    path('admin/orders/confirm-writeoff/', confirm_writeoff),
    path('admin/orders/pending-stock-returns/', pending_stock_returns),
    path('admin/orders/confirm-stock-returns/', confirm_stock_returns),
    path('admin/orders/invoice/process/', process_invoice),
    path('admin/orders/invoice/confirm/', confirm_invoice),
    path('admin/orders/invoice/revert/', revert_invoice),
    path('admin/orders/invoice/search-variants/', search_variants),
    path('admin/orders/search-bundles/', search_bundles),
    path('admin/orders/bundle/<int:bundle_id>/items/', bundle_items),
    path('admin/orders/generate-docx/', generate_docx),
    path('admin/orders/export-file/<str:token>/<str:kind>/', export_file),
    path('admin/orders/receipt-pdf/<int:order_id>/', order_receipt_pdf),
    path('admin/orders/payment-link/<int:order_id>/', admin_order_payment_link),
    path('admin/orders/update-field/', update_order_field),
    # "Analytics" dashboard: a clean URL (not under ModelAdmin /order/). The logic is
    # the method OrderAdmin.analytics_view; staff protection via admin.site.admin_view.
    path('admin/orders/analytics/', admin.site.admin_view(
        lambda request: admin.site._registry[Order].analytics_view(request)),
        name='orders_analytics'),
    path('admin/blog/fetch-youtube-info/', fetch_youtube_info),
    # Public API
    path('api/nova-poshta/', nova_poshta_public_proxy),
    path('api/orders/create/', create_order),
    path('api/monobank/webhook/', monobank_webhook),
    # Mono Personal API (sole-proprietor statements): URL with a secret instead of a signature.
    path('api/webhooks/monobank-personal/<str:secret>/', monobank_personal_webhook),
    path('api/categories/', categories_list),
    path('api/categories/flat/', categories_flat),
    path('api/products/', products_list),
    path('api/products/facets/', product_facets),
    path('api/products/<slug:slug>/', product_detail),
    path('api/products/<slug:slug>/bought-together/', bought_together),
    path('api/products/<slug:slug>/courses/', product_courses),
    path('api/courses/', courses_list),
    path('api/courses/<slug:slug>/', course_detail),
    path('api/courses/<slug:slug>/lessons/<int:lesson_id>/', lesson_content),
    # Paid video only via a signed token (nginx does not serve /media/courses/lessons/).
    path('api/courses/video/<str:token>/', course_video),
    path('api/me/courses/', my_courses),
    path('api/bundles/', bundles_list),
    path('api/bundles/<slug:slug>/', bundle_detail),
    # Flora plant-assistant (MyPlantApp) — read-only, secret-gated feed
    path('api/internal/flora/catalog/', flora_catalog_batch),
    path('api/internal/flora/catalog/delta/', flora_catalog_delta),
    path('api/reviews/', site_reviews),
    path('api/reviews/<int:review_id>/reply/', reply_to_review),
    path('api/products/<slug:slug>/reviews/', product_reviews),
    path('api/bundles/<slug:slug>/reviews/', bundle_reviews),
    path('api/brands/', brands_list),
    path('api/stock-notify/', notify_stock),
    path('api/favorites/set/', favorite_set),
    path('api/favorites/sync/', favorite_sync),
    path('api/search/', search),
    path('api/maintenance/', maintenance_status),
    path('api/site-settings/', site_settings),
    path('api/antibot-token/', antibot_token),
    path('api/auth/register/', register),
    path('api/auth/finalize-guest/', finalize_guest),
    path('api/auth/login/', login),
    # Refreshing the access token via refresh (ROTATE_REFRESH_TOKENS=True -> also returns a new
    # refresh)
    path('api/auth/refresh/', TokenRefreshView.as_view()),
    path('api/auth/me/', me),
    path('api/auth/logout/', logout),
    path('api/auth/profile/', update_profile),
    path('api/auth/password-reset/', password_reset_request),
    path('api/auth/password-reset/confirm/', password_reset_confirm),
    path('api/orders/my/', my_orders),
    path('api/orders/my/<str:order_number>/instruction/<int:item_id>/', order_item_instruction),
    path('api/orders/<str:order_number>/status/', order_public_status),
    path('api/orders/<str:order_number>/pay/', pay_order),
    path('api/orders/<str:order_number>/payment-method/', change_payment_method),
    path('api/orders/<str:order_number>/mark-paid/', mark_order_paid),
    path('api/orders/<str:order_number>/request-details/', request_payment_details),
    path('api/orders/<str:order_number>/screenshot/', upload_payment_screenshot),

    # Blog
    path('api/blog/', include('blog.urls')),

    # Chat (customer-facing REST)
    path('api/chat/', include((chat_public_patterns, 'chat'), namespace='chat_public')),

    # Chat (admin/CRM)
    path('admin/chat/', include((chat_admin_patterns, 'chat_admin'), namespace='chat_admin')),

    path('admin/', admin.site.urls),
    path('summernote/', include('django_summernote.urls')),

    # ── SEO ──────────────────────────────────────────────────────────────────
    path('robots.txt', robots_txt),
    path('sitemap.xml', cache_page(3600)(sitemap_index),
         {'sitemaps': SITEMAPS, 'sitemap_url_name': 'sitemap-section'}),
    path('sitemap-<section>.xml', cache_page(3600)(sitemap_view),
         {'sitemaps': SITEMAPS}, name='sitemap-section'),
    # Shell (catch-all): all navigation HTML paths of the frontend -> index.html with the SEO
    # <head>.
    # We exclude backend prefixes so as not to serve HTML for nonexistent api/admin/etc.
    re_path(r'^(?!api/|admin/|summernote/|media/|static/|ws/|sitemap|robots\.txt)',
            shell_view),
]

if settings.DEBUG:
    from django.urls import re_path
    from core.media import serve_media
    # Range-aware serving of media (video seeking). static() cannot do this.
    urlpatterns += [
        re_path(
            r'^media/(?P<path>.*)$', serve_media,
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]
