# Publish a public portfolio website (GitHub Pages)

The local link `http://127.0.0.1:8765/` is only for your PC. GitHub Pages gives you a publicly shareable link `https://YOUR_USERNAME.github.io/portfolio/`.

## Export only safe public files

From the installed project folder in PowerShell:

```powershell
.\Export-Public-Portfolio.ps1 -IncludeHistory
```

This makes a new sibling folder named `portfolio-public-export-YYYYMMDD-HHMMSS`. The only files copied are the dashboard, its Base configuration, the read-only snapshot script, the example data and the GitHub Pages workflow. With `-IncludeHistory` it also copies your existing `data.json` to preserve the **currently detected LP ranges and historical values**. Those balances and history will be PUBLIC. Omit `-IncludeHistory` to start a new public history.

**Do not upload your original entire local project.** Never upload `.env`, any API key, a seed phrase, or private keys. The public export intentionally excludes `.env`, `.backups`, local server scripts and unrelated files. Inspect the export before publishing.

## GitHub setup (once)

1. At GitHub, create an **empty public repository** called `portfolio`. Leave the default README and other initialization options OFF, so you have an empty repo.
2. Upload the *contents* of the new `portfolio-public-export-...` directory to the repo's `main` branch, keeping the `.github/workflows` and `scripts` subfolders. Do not upload the enclosing folder as one directory.
3. In **Settings → Secrets and variables → Actions**, create repository secrets `ALCHEMY_KEY` and `WALLET_ADDRESS`. Enter the same key and **public** `0x` wallet address as on your PC. Never upload `.env`.
4. In **Settings → Pages → Build and deployment → Source**, choose **GitHub Actions** (**not Deploy from a branch**).
5. In **Actions → Daily portfolio snapshot → Run workflow**, run it once. The workflow builds `data.json` if absent and publishes only the website. Find the actual public URL in **Settings → Pages → Visit site**.
6. Subsequent snapshots are scheduled at **16:30 America/New_York** daily, approximately 30 minutes after the regular US close; actual GitHub Actions starts may be delayed. Opening the website or switching currency never requests blockchain data. You may also manually rerun the workflow.

If the workflow cannot push its updated `data.json`, check **Settings → Actions → General → Workflow permissions** and the repository/organization action policies. The workflow requests `contents: write` and Pages deployment permissions.

The portfolio page, its token quantities, dollar values and history are public. The GitHub repository is public too, and `data.json` can be downloaded directly. Treat the URL as public information, not password-protected sharing.

The website's USD/GBP converter uses a public daily exchange-rate API. This is separate from Alchemy and does not spend Alchemy requests.

If your historical LP ranges were found on your PC, keep `-IncludeHistory` so the GitHub version can reuse the discovery cache; a new scan may miss old ranges.

### V10 performance chart on a public site

The site now also publishes `performance.json` created by `scripts/performance.py` during the daily GitHub workflow. The Treasury and S&P 500 series are fetched from FRED; these **do not use Alchemy**. The portfolio performance line is intentionally hidden unless the complete external-deposit/withdrawal record is available.

For a private cash-flow ledger, add GitHub Actions repository secret **`PORTFOLIO_CASHFLOWS_JSON`**, with the full contents of your local `cashflows.json`. Update this secret whenever you record a new external deposit/withdrawal. Do **not** upload `cashflows.json` into a public repository unless you intend to disclose it. `performance.json` (the calculated percentages) is published, but the secret and raw ledger are not. A public repo does make portfolio amounts/history in `data.json` visible.

The S&P 500 source is the **price-only** FRED SP500 series, excluding dividends. It is third-party copyrighted (*pre-approval required* for some uses); check its permitted uses before publishing the benchmark chart commercially. Treasury rates use FRED DGS3MO. References: https://fred.stlouisfed.org/series/SP500 , https://fred.stlouisfed.org/series/DGS3MO .
