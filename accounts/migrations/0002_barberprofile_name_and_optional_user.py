# Generated manually for standalone barber names (no login required)

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def fill_barber_names(apps, schema_editor):
    BarberProfile = apps.get_model("accounts", "BarberProfile")
    for bp in BarberProfile.objects.exclude(user_id__isnull=True).iterator():
        u = bp.user
        fn = (u.first_name or "").strip()
        ln = (u.last_name or "").strip()
        full = f"{fn} {ln}".strip() or (u.username or "")
        bp.name = full[:120]
        bp.save(update_fields=["name"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="barberprofile",
            name="name",
            field=models.CharField(blank=True, default="", max_length=120, verbose_name="اسم الحلاق"),
        ),
        migrations.RunPython(fill_barber_names, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="barberprofile",
            name="user",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="barber_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
