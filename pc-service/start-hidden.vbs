' Start the PC service with a hidden console window.
' Uses python.exe (not pythonw.exe) so the existing firewall allow rule still matches.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
py = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\python.exe")

If Not fso.FileExists(py) Then
    py = "python.exe"
End If

sh.CurrentDirectory = root
sh.Run """" & py & """ """ & root & "\run.py""", 0, False
