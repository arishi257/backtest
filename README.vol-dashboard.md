# Vol Dashboard

Multi-expiry NIFTY/SENSEX volatility dashboard built on the same option-chain,
analytics, and vol-curve logic used by `fit_sensex`.

## Setup

From `C:\Users\rishi\projects\vol dashboard`:

```powershell
..\.venv\Scripts\python.exe -m pip install -e .
```

Set Kite credentials through environment variables:

```powershell
$env:KITE_API_KEY="your_api_key"
$env:KITE_ACCESS_TOKEN="your_access_token"
```

Run:

```powershell
..\.venv\Scripts\python.exe -m vol_dashboard
```

The app reads `hols.xlsx` from this folder. The `Expiries` tab should contain:

- column A: `Index`, with values like `NIFTY` or `SENSEX`
- column B: `Expiry`, as an Excel date or text date

Each expiry becomes its own dashboard tab, for example `Nifty 19-May-26`.
