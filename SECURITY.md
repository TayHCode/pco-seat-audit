# Security

Do not commit Planning Center tokens, account exports, or personal information. Use `PCO_APP_ID` and `PCO_SECRET` environment variables only for a local live run, and keep live mode off unless you deliberately supply credentials. The API client is intended to issue GET requests only.

Report a security concern through the repository hosting platform's private vulnerability reporting feature in its Security area. Include reproduction steps without tokens or account exports. Do not put security details or credentials in a public issue.

A Personal Access Token has the creator’s full account access and cannot be scoped. This tool sends GET requests only. Setup keeps the application id and secret in memory for that process, never in files or reports. Delete the token in the developer account’s Personal Access Tokens section to revoke it. Keep pasted billing pages, real exports, reports and credentials outside version control.
