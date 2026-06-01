"""
اختبارات الشفت — واحد مفتوح، فتح جديد بعد الإغلاق
"""

from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from core.models import CloseLedger, CloseType, Shift
from core.shift_utils import daily_shift_name, get_open_shift, open_shift
from django.contrib.auth import get_user_model

User = get_user_model()


class DailyShiftTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="cashier", password="pass123", role="CASHIER"
        )

    def test_only_one_open_shift(self):
        s1 = open_shift(opened_by=self.user)
        s2 = get_open_shift()
        self.assertEqual(s1.id, s2.id)
        with self.assertRaises(ValidationError):
            open_shift(opened_by=self.user)

    def test_can_open_new_shift_after_close_same_day(self):
        shift = open_shift(opened_by=self.user)
        shift.is_closed = True
        shift.ended_at = timezone.now()
        shift.save(update_fields=["is_closed", "ended_at", "updated_at"])
        self.assertIsNone(get_open_shift())
        shift2 = open_shift(opened_by=self.user)
        self.assertFalse(shift2.is_closed)
        self.assertNotEqual(shift.id, shift2.id)
        self.assertIn("(2)", shift2.name)

    def test_daily_name_sequence(self):
        Shift.objects.all().delete()
        first = open_shift(opened_by=self.user)
        self.assertEqual(first.name, f"شفت {timezone.localdate():%d/%m/%Y}")
        first.is_closed = True
        first.ended_at = timezone.now()
        first.save(update_fields=["is_closed", "ended_at"])
        second = open_shift(opened_by=self.user)
        self.assertIn("(2)", second.name)


class CloseLedgerTest(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(
            username="admin", password="pass123", role="ADMIN"
        )
        self.shift = Shift.objects.create(name=daily_shift_name())

    def test_close_ledger_with_user(self):
        ledger = CloseLedger.objects.create(
            close_type=CloseType.SHIFT,
            shift=self.shift,
            closed_by=self.admin_user,
            total_revenue=200,
            total_cash=200,
            note="إغلاق يدوي",
        )
        self.assertEqual(ledger.closed_by, self.admin_user)
