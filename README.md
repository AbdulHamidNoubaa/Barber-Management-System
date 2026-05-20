# Barber Management System (Django)

نظام إدارة محل حلاقة (Backend مبدئياً بدون API) مبني بـ Django مع صلاحيات (Admin/Cashier/Barber) ودعم الطابور والمدفوعات والإغلاق اليومي، عبر Django Admin ولوحات داخلية لاحقاً.

## التشغيل السريع (Windows / PowerShell)

```powershell
.\venv\Scripts\python -m pip install -r .\requirements.txt
.\venv\Scripts\python -m django --version

# إنشاء المشروع وتشغيله (بعد ما يتم توليد الكود)
.\venv\Scripts\python manage.py migrate
.\venv\Scripts\python manage.py createsuperuser
.\venv\Scripts\python manage.py runserver
```

