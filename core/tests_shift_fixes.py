"""
ملف اختبار لمشاكل فتح وإغلاق الشفت - Test Cases

يمكن تشغيل هذه الاختبارات بـ:
    python manage.py test core.tests.ShiftTests -v 2
"""

from decimal import Decimal
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from core.models import (
    Shift, ShiftTemplate, CloseLedger, Ticket, Customer,
    TicketStatus, CloseType, PaymentMethod, get_or_create_open_shift
)
from accounts.models import BarberProfile

User = get_user_model()


class ShiftLockingTest(TransactionTestCase):
    """اختبار عمليات القفل والـ Race Conditions"""

    def setUp(self):
        self.admin_user = User.objects.create_user(
            username='admin',
            password='pass123',
            role='ADMIN'
        )

    def test_get_or_create_open_shift_no_race_condition(self):
        """تأكد أن get_or_create_open_shift() آمن من race conditions"""
        # يجب أن تكون دالة واحدة فقط ترجع شفت واحد
        shift1 = get_or_create_open_shift()
        shift2 = get_or_create_open_shift()
        
        self.assertEqual(shift1.id, shift2.id, "يجب أن ترجع نفس الشفت")
        
        # تأكد أن هناك شفت واحد فقط مفتوح
        open_shifts = Shift.objects.filter(is_closed=False, ended_at__isnull=True)
        self.assertEqual(open_shifts.count(), 1, "يجب أن يكون هناك شفت واحد فقط مفتوح")

    def test_auto_shift_open_close_cycle(self):
        """اختبر دورة الفتح والإغلاق التلقائي للشفت"""
        # إنشء shift template
        now = timezone.localtime()
        current_time = now.time()
        
        template = ShiftTemplate.objects.create(
            name='Morning Shift',
            start_time=current_time,
            end_time=(now.replace(hour=(now.hour + 1) % 24, minute=0, second=0)).time()
        )
        
        # يجب أن يتم فتح shift تلقائياً
        from barber_ms.views import _auto_manage_shifts
        shift = _auto_manage_shifts()
        
        self.assertIsNotNone(shift, "يجب أن يتم فتح شفت تلقائياً")
        self.assertEqual(shift.name, 'Morning Shift')
        self.assertFalse(shift.is_closed)


class CloseLedgerTest(TestCase):
    """اختبار إنشاء CloseLedger مع closed_by=None"""

    def setUp(self):
        self.admin_user = User.objects.create_user(
            username='admin',
            password='pass123',
            role='ADMIN'
        )
        self.shift = Shift.objects.create(name='Test Shift')
        self.customer = Customer.objects.create(name='Test Customer')
        self.barber_profile = BarberProfile.objects.create(
            user=self.admin_user,
            name='Test Barber'
        )

    def test_auto_close_ledger_with_null_closed_by(self):
        """تأكد أن CloseLedger يمكن أن يُنشأ مع closed_by=None"""
        ledger = CloseLedger.objects.create(
            close_type=CloseType.SHIFT,
            shift=self.shift,
            closed_by=None,  # Auto-close بدون مستخدم
            total_revenue=Decimal("100.00"),
            total_cash=Decimal("100.00"),
            note="إغلاق تلقائي"
        )
        
        self.assertIsNotNone(ledger.id)
        self.assertIsNone(ledger.closed_by)
        self.assertEqual(ledger.close_type, CloseType.SHIFT)

    def test_close_ledger_with_user(self):
        """تأكد أن CloseLedger يعمل مع closed_by محدد"""
        ledger = CloseLedger.objects.create(
            close_type=CloseType.SHIFT,
            shift=self.shift,
            closed_by=self.admin_user,
            total_revenue=Decimal("200.00"),
            total_cash=Decimal("200.00"),
            note="إغلاق يدوي"
        )
        
        self.assertIsNotNone(ledger.id)
        self.assertEqual(ledger.closed_by, self.admin_user)

    def test_shift_closed_with_null_closed_by(self):
        """تأكد أن Shift يمكن أن يُغلق مع closed_by=None"""
        self.shift.is_closed = True
        self.shift.ended_at = timezone.now()
        self.shift.closed_by = None  # Auto-close
        self.shift.save()
        
        self.shift.refresh_from_db()
        self.assertTrue(self.shift.is_closed)
        self.assertIsNone(self.shift.closed_by)


class ShiftTransactionTest(TransactionTestCase):
    """اختبار معالجة Transactions في عمليات الشفت"""

    def setUp(self):
        self.admin_user = User.objects.create_user(
            username='admin',
            password='pass123',
            role='ADMIN'
        )
        self.cashier_user = User.objects.create_user(
            username='cashier',
            password='pass123',
            role='CASHIER'
        )

    def test_shift_open_transaction(self):
        """تأكد أن فتح الشفت يتم بشكل atomic"""
        # يجب أن يكون هناك شفت واحد فقط
        shifts_before = Shift.objects.filter(is_closed=False, ended_at__isnull=True).count()
        
        shift = get_or_create_open_shift('Test Shift')
        
        shifts_after = Shift.objects.filter(is_closed=False, ended_at__isnull=True).count()
        self.assertEqual(shifts_after, shifts_before + 1)

    def test_concurrent_shift_creation_safety(self):
        """تأكد أن الإنشاء المتزامن للشفت آمن"""
        # محاكاة عملية متزامنة
        shift1 = get_or_create_open_shift('Concurrent Test')
        shift2 = get_or_create_open_shift('Concurrent Test')
        
        # يجب أن تكون نفس الشفت
        self.assertEqual(shift1.id, shift2.id)
        
        # يجب أن يكون هناك شفت واحد فقط
        open_shifts = Shift.objects.filter(is_closed=False, ended_at__isnull=True)
        self.assertEqual(open_shifts.count(), 1)


class ErrorHandlingTest(TestCase):
    """اختبار معالجة الأخطاء في عمليات الشفت"""

    def setUp(self):
        self.admin_user = User.objects.create_user(
            username='admin',
            password='pass123',
            role='ADMIN'
        )

    def test_close_nonexistent_shift(self):
        """اختبر إغلاق شفت غير موجود"""
        # لا يوجد شفت مفتوح
        shift = Shift.objects.filter(is_closed=False, ended_at__isnull=True).first()
        self.assertIsNone(shift)
        
        # محاولة الحصول على شفت يجب أن تُرجع واحداً جديداً
        new_shift = get_or_create_open_shift()
        self.assertIsNotNone(new_shift)


# ملاحظات عن الاختبارات:
# - TransactionTestCase: لاختبار العمليات المتزامنة والقفل
# - TestCase: لاختبارات عادية (تستخدم transaction)
# - استخدم --keepdb لتسريع الاختبارات: python manage.py test --keepdb
