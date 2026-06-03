' تشغيل النظام بدون نافذة CMD (اختياري — أصعب في التشخيص)
' لإيقاف النظام استخدم stop_barber.bat

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectRoot = fso.GetParentFolderName(fso.GetParentFolderName(scriptDir))
batPath = fso.BuildPath(scriptDir, "start_barber.bat")

If Not fso.FileExists(batPath) Then
    MsgBox "لم يُعثر على start_barber.bat", vbCritical, "نظام الحلاقة"
    WScript.Quit 1
End If

' 0 = إخفاء النافذة
sh.Run """" & batPath & """", 0, False

WScript.Sleep 2500
sh.Run "http://127.0.0.1:8000/", 1, False
