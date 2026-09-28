@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
echo.
echo CONFIGURACAO DO GEMINI PARA O NFREADER
echo A chave sera salva somente neste computador, no arquivo back\.env.
echo.
set /p "GEMINI_KEY=Informe a GEMINI_API_KEY: "
if "%GEMINI_KEY%"=="" (
    echo.
    echo ERRO: nenhuma chave foi informada.
    pause
    exit /b 1
)
> ".env" echo GEMINI_API_KEY=%GEMINI_KEY%
>> ".env" echo GEMINI_MODEL=gemini-3.5-flash-lite
>> ".env" echo GEMINI_FALLBACK_MODELS=gemini-3.6-flash,gemini-3.5-flash
>> ".env" echo NFREADER_GEMINI_TIMEOUT=90
>> ".env" echo NFREADER_GEMINI_RETRIES=4
>> ".env" echo NFREADER_GEMINI_RETRY_DELAY=5
>> ".env" echo NFREADER_GEMINI_PAGE_DELAY=8
>> ".env" echo NFREADER_QUEUE_RETRY_DELAYS=30,60,120,180
>> ".env" echo NFREADER_QUEUE_MAX_ATTEMPTS=0
echo.
echo Configuracao salva. Reinicie o backend do NFreader.
pause
