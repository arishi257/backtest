# Backtest

Offline NIFTY option replay project for the copied `vol_dashboard` analytics and UI.

The replay reads the GFDL minute CSV, keeps only `NIFTY` option instruments, floors timestamps to the minute, and forward-fills each instrument independently. At each market minute from 09:15 to 15:30 IST it feeds close prices as both bid and ask into the dashboard analytics.

## Setup

```powershell
C:\Users\rishi\my_project\.venv\Scripts\python.exe -m pip install -e .
```

## Dashboard Replay

```powershell
C:\Users\rishi\my_project\.venv\Scripts\python.exe -m backtest
```

Or by sample file date:

```powershell
C:\Users\rishi\my_project\.venv\Scripts\python.exe -m backtest --date 09042026
```

Accepted date formats include `09042026`, `09-04-2026`, and `2026-04-09`.

You can still use an explicit CSV:

```powershell
C:\Users\rishi\my_project\.venv\Scripts\python.exe -m backtest --csv "C:\Users\rishi\OneDrive\Desktop\sample data\GFDLNFO_BACKADJUSTED_09042026\GFDLNFO_BACKADJUSTED_09042026.csv"
```

## Headless Run

```powershell
C:\Users\rishi\my_project\.venv\Scripts\python.exe -m backtest.runner
```

Headless output is written to `backtest_snapshots`.
