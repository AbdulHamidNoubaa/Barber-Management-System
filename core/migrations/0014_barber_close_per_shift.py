# Generated manually — إغلاق الحلاق per-shift بدل unique per calendar day

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_service_name_non_unique"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="barberdailyclose",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="barberdailyclose",
            constraint=models.UniqueConstraint(
                condition=models.Q(("shift__isnull", False)),
                fields=("barber", "shift"),
                name="uq_barber_daily_close_per_shift",
            ),
        ),
    ]
