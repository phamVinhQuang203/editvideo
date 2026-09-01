' Bat Video Studio CHAY NGAM (khong hien cua so CMD). Mo http://127.0.0.1:7995
' Chon pythonw: chay Python khong tao cua so console.
Dim fso, root, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
If fso.FileExists(root & "\venv\Scripts\pythonw.exe") Then
  pyw = root & "\venv\Scripts\pythonw.exe"
ElseIf fso.FileExists("E:\CODEX\autovoice-tool\.venv\Scripts\pythonw.exe") Then
  pyw = "E:\CODEX\autovoice-tool\.venv\Scripts\pythonw.exe"
Else
  pyw = "pythonw.exe"
End If
CreateObject("WScript.Shell").Run Chr(34) & pyw & Chr(34) & " " & Chr(34) & root & "\app\server.py" & Chr(34), 0, False
