with open('core/i18n.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('"Smart Browser": "المتصفح الذكي"', '"Smart Browser": "المتصفح"')
text = text.replace('"Bulk Download": "التحميل المتعدد"', '"Bulk Download": "تحميل متعدد"')
text = text.replace('"Playlists": "قوائم التشغيل"', '"Playlists": "قوائم"')
text = text.replace('"Subscriptions": "الاشتراكات"', '"Subscriptions": "اشتراكات"')
text = text.replace('"Downloads": "التحميلات"', '"Downloads": "تحميلات"')
text = text.replace('"Tools": "الأدوات"', '"Tools": "أدوات"')
text = text.replace('"Stats": "الإحصاءات"', '"Stats": "إحصاءات"')
text = text.replace('"Errors": "الأخطاء"', '"Errors": "أخطاء"')
text = text.replace('"Settings": "الإعدادات"', '"Settings": "إعدادات"')

with open('core/i18n.py', 'w', encoding='utf-8') as f:
    f.write(text)

print('Updated translations')
