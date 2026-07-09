# Открывает публичный HTTPS-туннель к локальному боту через localtunnel (lt).
# Нужен, чтобы Restoplace мог достучаться до вебхука с интернета.
#
# Требования: Node.js + пакет localtunnel (npm i -g localtunnel).
# Скрипт сам поставит localtunnel, если его нет.
#
# Использование:
#   ./run-lt.ps1                              # порт 8000, случайный поддомен
#   ./run-lt.ps1 -Subdomain lime-max          # стабильный адрес lime-max.loca.lt
#   ./run-lt.ps1 -Port 8000 -Subdomain lime-max
#
# Порядок запуска:
#   1) в одном окне:  ./run.ps1        (поднимает бота на http://localhost:8000)
#   2) в другом окне: ./run-lt.ps1     (открывает туннель к нему)
#   3) в Restoplace вставьте адрес туннеля + /webhook/restoplace?token=WEBHOOK_SECRET

param(
    [int]$Port = 8000,
    [string]$Subdomain = ""
)
$ErrorActionPreference = "Stop"

# Проверяем, что localtunnel установлен; если нет — ставим глобально.
if (-not (Get-Command lt -ErrorAction SilentlyContinue)) {
    Write-Host "localtunnel не найден — устанавливаю (npm i -g localtunnel)..." -ForegroundColor Yellow
    npm install -g localtunnel
}

# Стабильный поддомен удобен: адрес не меняется между перезапусками,
# и не приходится каждый раз переписывать webhook в Restoplace.
$ltArgs = @("--port", $Port)
if ($Subdomain) { $ltArgs += @("--subdomain", $Subdomain) }

Write-Host "Открываю туннель к http://localhost:$Port ..." -ForegroundColor Green
Write-Host "Ниже появится публичный адрес вида https://ИМЯ.loca.lt" -ForegroundColor Cyan
Write-Host "Webhook для Restoplace: АДРЕС-ТУННЕЛЯ + /webhook/restoplace?token=WEBHOOK_SECRET из .env" -ForegroundColor Cyan
Write-Host ""

lt @ltArgs
