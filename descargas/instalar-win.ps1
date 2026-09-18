# Lógica de instalación de DiagraMind Local (Windows).
# La invoca Instalar-DiagraMinder-Backend-win.bat. Descarga el .exe, lo instala y
# crea un acceso en la carpeta de Inicio para que arranque solo al iniciar sesión.
$ErrorActionPreference = "Stop"

$url = "https://github.com/JuanseGZZ/diagraminder-backend/releases/latest/download/DiagraMinder-Backend-win.exe"
$dir = Join-Path $env:LOCALAPPDATA "DiagraMind"
$bin = Join-Path $dir "DiagraMinder-Backend.exe"

New-Item -ItemType Directory -Force -Path $dir | Out-Null

Write-Host "Descargando el programa..."
Invoke-WebRequest -Uri $url -OutFile $bin

Write-Host "Configurando auto-inicio..."
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup "DiagraMind Local.lnk"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $bin
$sc.Arguments = "--no-ui"
$sc.WindowStyle = 7   # minimizado
$sc.Save()

# arrancarlo ahora
Start-Process -FilePath $bin -ArgumentList "--no-ui"
Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:8765"

Write-Host ""
Write-Host "Listo. DiagraMind Local corre en http://127.0.0.1:8765 y arranca solo al iniciar Windows."
Write-Host "Volve a la web y toca 'Conectar local'."
