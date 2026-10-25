@echo off
rem ============================================================
rem  Enable/disable HTTPS port-forward (443) on the router
rem  Target: X-WRT / OpenWrt (e.g. Xiaomi Mi Router 3G v1)
rem
rem  Usage (run on a Windows PC inside that router's LAN):
rem      enable_443_forward.bat -check    read-only: list existing forwards (run this first)
rem      enable_443_forward.bat           add 443 -> SERVER:443, cloned from the existing 80 rule
rem      enable_443_forward.bat -remove   delete the rule this script added
rem
rem  Notes:
rem   * Only ONE new rule named portal443 is added; existing rules are never touched.
rem   * WAN zone / target IP are cloned from the existing port-80 redirect (not guessed).
rem   * No scp needed: the remote script is passed as one argument to ssh (one password
rem     prompt). All messages are ASCII on purpose - Chinese text inside a .bat gets
rem     garbled by the Windows console codepage (GBK vs UTF-8).
rem ============================================================
setlocal enabledelayedexpansion
set ROUTER_USER=root
rem Built-in default router IP (confirmed by the maintainer on 2026-09-11).
rem Override with:  -gw <ip>   or set the ROUTER_IP environment variable.
set ROUTER_DEFAULT=192.168.1.1
set SSH_OPTS=-o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile="%TEMP%\portal443_known_hosts"

set GWARG=
set MODE=apply
if /i "%~1"=="-check"  set MODE=check
if /i "%~1"=="/check"  set MODE=check
if /i "%~1"=="--check" set MODE=check
if /i "%~1"=="-remove" set MODE=remove
if /i "%~1"=="/remove" set MODE=remove
if /i "%~1"=="--remove" set MODE=remove
if /i "%~1"=="-gw"     set GWARG=%~2
if /i "%~1"=="/gw"     set GWARG=%~2
if /i "%~1"=="-router" set GWARG=%~2
if defined ROUTER_IP set GWARG=%ROUTER_IP%

set "PAYLOAD_CHECK=echo --- existing port forwards ---; uci show firewall 2>/dev/null | grep -F .src_dport= | head -30; echo --- portal443 ---; uci show firewall.portal443 2>/dev/null || echo not-created-yet; echo --- kernel rules with 443 ---; nft list ruleset 2>/dev/null | grep -c 443 || true; iptables-save 2>/dev/null | grep -c 443 || true"
set "PAYLOAD_REMOVE=uci -q delete firewall.portal443; uci commit firewall; /etc/init.d/firewall reload >/dev/null 2>&1; echo removed:; uci show firewall.portal443 2>/dev/null || echo gone"
set "PAYLOAD_APPLY=BASE=; for s in $(uci show firewall 2>/dev/null | grep -F .src_dport= | cut -d. -f2 | cut -d= -f1 | sort -u); do ports=$(uci get firewall.$s.src_dport 2>/dev/null | tr -d '\047'); for p in $ports; do if [ $p = 80 ]; then BASE=$s; break 2; fi; done; done; if [ -z $BASE ]; then echo NO_TEMPLATE-no-port-80-redirect-on-this-router; echo hint-check-where-80-is-forwarded-or-add-via-LuCI; exit 3; fi; ZONE=$(uci get firewall.$BASE.src 2>/dev/null); DEST=$(uci get firewall.$BASE.dest 2>/dev/null); DESTIP=$(uci get firewall.$BASE.dest_ip 2>/dev/null); echo TEMPLATE=$BASE src=$ZONE dest=$DEST dest_ip=$DESTIP; if [ -z $DESTIP ]; then echo NO_DESTIP-template-has-no-dest_ip; exit 4; fi; uci -q delete firewall.portal443; uci set firewall.portal443=redirect; uci set firewall.portal443.name=portal-https; uci set firewall.portal443.target=DNAT; uci set firewall.portal443.src=$ZONE; uci set firewall.portal443.src_dport=443; uci set firewall.portal443.dest=$DEST; uci set firewall.portal443.dest_ip=$DESTIP; uci set firewall.portal443.dest_port=443; uci set firewall.portal443.proto=tcp; uci commit firewall; /etc/init.d/firewall reload >/dev/null 2>&1; echo WROTE:; uci show firewall.portal443"

echo ============================================
echo  Portal 443 port-forward helper (X-WRT/OpenWrt)
echo  mode: %MODE%
echo ============================================

where ssh >nul 2>nul
if errorlevel 1 (
  echo [x] ssh.exe not found. Install the OpenSSH Client optional feature:
  echo     Settings ^> Apps ^> Optional features ^> Add ^> OpenSSH Client
  echo     or PowerShell: Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
  pause & exit /b 1
)

set GW=%GWARG%
if defined GW goto :gw_ready

rem Built-in default first: it is a known-good value on this LAN, so no probing needed.
set GW=%ROUTER_DEFAULT%
ping -n 1 -w 1200 %GW% >nul 2>nul
if not errorlevel 1 (
  echo [i] using built-in default router %GW%
  goto :gw_ready
)
echo [!] built-in default %GW% did not answer ping; falling back to auto-detection...
set GW=

rem Probe 1: route print (language independent, no PowerShell policy issues)
set DET_RT=
for /f "tokens=1,2,3" %%a in ('route print -4 ^| findstr /r /c:"^ *0\.0\.0\.0"') do (
  if not defined DET_RT set DET_RT=%%c
)
call :clean DET_RT
if defined DET_RT set GW=%DET_RT%

rem Probe 2: PowerShell (fallback)
if not defined GW (
  set DET_PS=
  for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "$r=Get-NetRoute -DestinationPrefix 0.0.0.0/0 ^| Sort-Object RouteMetric ^| Select-Object -First 1; if ($r) { [string]$r.NextHop }" 2^>nul`) do set DET_PS=%%a
  call :clean DET_PS
  if defined DET_PS set GW=%DET_PS%
)

if not defined GW (
  echo [x] Cannot detect the router IP automatically.
  echo     route print probe : [!DET_RT!]
  echo     PowerShell probe  : [!DET_PS!]
  echo.
  echo     Fix: pass the router IP explicitly, e.g.
  echo          enable_443_forward.bat -gw 192.168.1.1        (add -check to only inspect)
  echo     Read it from:  ipconfig  ->  "Default Gateway"
  pause & exit /b 1
)

:gw_ready
rem Validate: must be an IPv4 literal (never hand an empty/bogus value to ssh)
echo %GW%| findstr /r /c:"^[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*$" >nul
if errorlevel 1 (
  echo [x] Invalid router address: [%GW%]
  echo     Expecting an IPv4 address such as 192.168.1.1. Use: -gw ^<ip^>
  pause & exit /b 1
)
echo [i] router: %GW%
ping -n 1 -w 1500 %GW% >nul 2>nul
if errorlevel 1 echo [!] ping %GW% failed - continue anyway, but check you are on the router LAN.
echo [i] ssh user: %ROUTER_USER%   (the router root password is asked once)
echo.
goto :after_gw

:clean
rem Trim spaces and invisible chars; %~1 is the variable NAME (needs delayed expansion)
set "_v=!%~1!"
if not defined _v exit /b 0
for /f "tokens=* delims= " %%x in ("!_v!") do set "_v=%%x"
set "%~1=!_v!"
exit /b 0

:after_gw

if /i "%MODE%"=="check"  set "PAYLOAD=%PAYLOAD_CHECK%"
if /i "%MODE%"=="remove" set "PAYLOAD=%PAYLOAD_REMOVE%"
if /i "%MODE%"=="apply"  set "PAYLOAD=%PAYLOAD_APPLY%"

echo === connecting to the router and running the remote script ===
ssh %SSH_OPTS% %ROUTER_USER%@%GW% "%PAYLOAD%"
set RC=%errorlevel%
echo [i] remote exit code: %RC%

if /i "%MODE%"=="apply" if "%RC%"=="0" (
  echo.
  echo === local reachability probe ===
  echo     NOTE: many routers do not support NAT loopback, so probing from inside the
  echo     LAN may fail even when the forward is correct. Verify from outside the LAN
  echo     or via mobile data:  https://portal.example.com/
  powershell -NoProfile -Command "try { $r = Test-NetConnection -ComputerName portal.example.com -Port 443 -WarningAction SilentlyContinue; Write-Host ('portal.example.com:443 reachable -> ' + $r.TcpTestSucceeded) } catch { Write-Host 'probe failed (see NOTE above)' }"
  echo.
  echo Forwarding only makes the port reachable; the server must also serve TLS:
  echo     sudo /opt/portal/.venv/bin/python /opt/portal/scripts/portal_tls.py enable
  echo To undo the forward:  enable_443_forward.bat -remove
)

if /i "%MODE%"=="apply" if not "%RC%"=="0" (
  echo.
  echo [!] The remote script failed. Fallback: add the forward in the router LuCI UI:
  echo     Network - Firewall - Port Forwards - Add:
  echo       name=portal-https  protocol=TCP  external zone=wan  external port=443
  echo       internal zone=lan  internal IP=192.168.1.130  internal port=443
  echo     (copy the fields from the existing port-80 rule)
)

echo.
pause
endlocal
