Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = dir & "\.venv\Scripts\pythonw.exe"
script = dir & "\voice_media.py"
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = dir
WshShell.Run """" & pythonw & """ """ & script & """", 0, False
