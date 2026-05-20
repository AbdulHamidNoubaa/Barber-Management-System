from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import BarberProfile, User, UserRole


@receiver(post_save, sender=User)
def ensure_barber_profile(sender, instance: User, created: bool, **kwargs):
    if instance.role == UserRole.BARBER:
        BarberProfile.objects.get_or_create(user=instance)
