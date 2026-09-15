# Empaqueta MyWhisper para repartir.
#
#   .\build.ps1              compila y arma el ZIP
#   .\build.ps1 -SoloExe     compila sin comprimir (mucho más rápido al iterar)
#
# El resultado queda en dist\MyWhisper-<version>.zip junto con su archivo de
# hash. Ese hash es lo que hay que mandarle a quien reciba el ZIP, POR UN CANAL
# DISTINTO al que usaste para mandar el ZIP: es la única forma que tiene de
# comprobar que lo que abre es lo que mandaste, ya que el .exe no está firmado.

param(
    [switch]$SoloExe
)

$ErrorActionPreference = "Stop"
$raiz   = $PSScriptRoot
$python = Join-Path $raiz "buildenv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Falta el entorno de compilación. Crealo con:`n  py -3.11 -m venv buildenv`n  .\buildenv\Scripts\pip install -r requirements.txt"
}

$version = (Select-String -Path (Join-Path $raiz "app\__init__.py") -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "== MyWhisper $version ==" -ForegroundColor Cyan

# El modelo no está en git (pesa 464 MB); se baja si falta.
if (-not (Test-Path (Join-Path $raiz "models\small\model.bin"))) {
    Write-Host "-- Descargando el modelo small (una sola vez)..." -ForegroundColor Yellow
    & $python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', revision='536b0662742c02347bc0e980a01041f333bce120', local_dir='models/small', allow_patterns=['*.bin','*.json','*.txt'])"
}

if (-not (Test-Path (Join-Path $raiz "assets\MyWhisper.ico"))) {
    Write-Host "-- Generando el icono..." -ForegroundColor Yellow
    & $python (Join-Path $raiz "tools\make_icon.py")
}

$salida = Join-Path $raiz "dist\MyWhisper"

# PyInstaller borra dist\MyWhisper entera antes de compilar. Ahí también viven
# los datos que la app genera al usarse: el pack CUDA (1,5 GB), los modelos
# descargados (hasta 3 GB), la config y el log. Se apartan antes y se devuelven
# al final, pase lo que pase. Mientras están apartados el ZIP sale limpio, sin
# datos personales ni gigas de NVIDIA.
$apartado = Join-Path $raiz "dist\_datos_locales"
$datosLocales = @("cuda", "models", "MyWhisper.config.json", "MyWhisper.log", "MyWhisper.log.1")

function Apartar-DatosLocales {
    if (-not (Test-Path $salida)) { return }
    New-Item -ItemType Directory -Force $apartado | Out-Null
    foreach ($d in $datosLocales) {
        $origen = Join-Path $salida $d
        if (Test-Path $origen) {
            Move-Item $origen (Join-Path $apartado $d) -Force
            Write-Host "   apartado: $d" -ForegroundColor DarkGray
        }
    }
}

function Devolver-DatosLocales {
    if (-not (Test-Path $apartado)) { return }
    New-Item -ItemType Directory -Force $salida | Out-Null
    foreach ($d in $datosLocales) {
        $origen = Join-Path $apartado $d
        if (Test-Path $origen) {
            Move-Item $origen (Join-Path $salida $d) -Force
        }
    }
    if (-not (Get-ChildItem $apartado -Force)) { Remove-Item $apartado }
    Write-Host "-- Datos locales devueltos a $salida" -ForegroundColor DarkGray
}

try {
Apartar-DatosLocales

Write-Host "-- Compilando..." -ForegroundColor Yellow
& $python -m PyInstaller --noconfirm --clean (Join-Path $raiz "MyWhisper.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló" }

Copy-Item (Join-Path $raiz "assets\LEEME.txt") $salida -Force

# Toda licencia de terceros que viaja en el paquete. Ver toolsecolectar_licencias.py.
& $python (Join-Path $raiz "toolsecolectar_licencias.py") $salida
if ($LASTEXITCODE -ne 0) { throw "No se pudieron recolectar las licencias" }

$mb = [math]::Round((Get-ChildItem $salida -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB)
Write-Host "-- Carpeta lista: $mb MB" -ForegroundColor Green

if ($SoloExe) {
    Write-Host "Listo (sin ZIP). Probalo con:`n  $salida\MyWhisper.exe" -ForegroundColor Cyan
    return
}

$zip = Join-Path $raiz "dist\MyWhisper-$version.zip"
Write-Host "-- Comprimiendo (esto tarda unos minutos)..." -ForegroundColor Yellow
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $salida -DestinationPath $zip -CompressionLevel Optimal

$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
$zipMb = [math]::Round((Get-Item $zip).Length / 1MB)
$notas = @"
MyWhisper $version
Archivo : $(Split-Path $zip -Leaf)
Tamano  : $zipMb MB
SHA-256 : $hash

Mandale este SHA-256 a quien reciba el ZIP por un canal DISTINTO al que
usaste para mandarle el archivo. Lo verifica con:

    Get-FileHash $(Split-Path $zip -Leaf) -Algorithm SHA256
"@
$notas | Out-File -FilePath (Join-Path $raiz "dist\SHA256.txt") -Encoding utf8

Write-Host ""
Write-Host $notas -ForegroundColor Green

} finally {
    Devolver-DatosLocales
}
