"""
URLs لـ VIP Bookings و Receipts و Treasury Reports
يتم تضمينها في frontend_urls.py
"""

from django.urls import path
from barber_ms import views_vip_receipt

app_name = 'vip'

urlpatterns = [
    # VIP Booking URLs
    path('vip-bookings/', views_vip_receipt.vip_bookings_list, name='vip_bookings_list'),
    path('vip-bookings/create/', views_vip_receipt.create_vip_booking, name='create_vip_booking'),
    path('vip-bookings/<int:booking_id>/', views_vip_receipt.vip_booking_detail, name='vip_booking_detail'),
    path('vip-bookings/<int:booking_id>/edit/', views_vip_receipt.edit_vip_booking, name='edit_vip_booking'),
    
    # Receipt URLs
    path('receipts/', views_vip_receipt.receipts_list, name='receipts_list'),
    path('receipts/generate/', views_vip_receipt.generate_receipt, name='generate_receipt'),
    path('receipts/<int:receipt_id>/print/', views_vip_receipt.receipt_print, name='receipt_print'),
    
    # Treasury Report URLs
    path('treasury/report/', views_vip_receipt.treasury_report, name='treasury_report'),
    path('treasury/summary/', views_vip_receipt.treasury_summary, name='treasury_summary'),
]
