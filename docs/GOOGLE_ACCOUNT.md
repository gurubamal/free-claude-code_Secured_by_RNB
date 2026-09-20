# Gemini with a Google account

The **Gemini / Google account** provider signs in through Google's browser OAuth flow for the public **Gemini Developer API**. No Gemini API key is needed. You do need a Google Cloud project and your own Desktop OAuth client, as required by [Google's OAuth setup guide](https://ai.google.dev/gemini-api/docs/oauth).

This connection uses your project's API quotas and billing. It does not import a Gemini CLI login or turn a Gemini consumer subscription into API access. Google [explicitly disallows piggybacking on Gemini CLI OAuth](https://geminicli.com/docs/resources/faq/) from third-party tools.

## One-time Google setup

1. Create or choose a project in [Google Cloud Console](https://console.cloud.google.com/). Copy its **project ID**, not its display name.
2. Enable the [Generative Language API](https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com) for that project. Your signed-in user must have permission to consume the API in the project.
3. Configure your app in [Google Auth Platform](https://console.cloud.google.com/auth/overview). For an External app in Testing, add your own Google account under **Audience → Test users**.
4. Under **Clients**, create a client with application type **Desktop app**. Copy the client ID and client secret. Use your own client; no shared working client credentials are shipped with this repository. The app handles the temporary loopback redirect port automatically.

## Connect in FCC

1. Open [local Admin](http://127.0.0.1:8082/admin) and sign in.
2. Under **Providers → OAuth providers**, find **Gemini / Google account** and choose **Edit**.
3. Enter **Google Desktop OAuth Client ID**, **Google OAuth Client Secret** and **Google Cloud Project ID**. Choose **Save**.
4. Choose **Sign in with Google**. Select the test/authorized account and review Google's consent screen. Authentication takes place on Google's website; FCC never asks for your Google password.
5. Return to Admin. **Connected** means OAuth credentials were saved. A successful catalog load is a separate check; neither alone proves inference capacity.
6. In **Routing controls**, enable **Allow paid API routes**, include this provider in your priority order and refresh catalogs. This switch gates Google OAuth even if your project currently has free quota: the gateway cannot establish free billing from a login. Set spending limits with Google.

Automatic routing requires reported catalog input capacity of **more than 512,000 tokens**, known tool support and enough room for the request. Missing metadata and smaller models remain excluded. The existing free-first/paid-later order, cooldowns and fallback rules apply. Messages, streaming Responses and Chat Completions can use this connection.

## Storage and troubleshooting

Access and refresh tokens stay in the private user configuration directory, outside the repository. Windows stores them with user-bound DPAPI protection. Tokens refresh automatically before expiry; revoked authorization requires a new sign-in. **Cancel sign-in** closes the pending callback listener. **Disconnect** removes local credentials and attempts Google revocation, reporting if revocation was not confirmed.

- **Configure your own client**: save all three fields, then sign in. Changing the client or project requires another sign-in.
- **Access blocked / invalid client**: check that the client is a Desktop app, its secret matches, and your Google account is an allowed test user.
- **Connected, but models unavailable / HTTP 403**: check that the API is enabled and that the account has project API-consumption permission. Sign-in does not grant IAM roles.
- **No eligible models**: inspect the catalog, 512k floor, tool metadata, paid-API switch and exclusions in Routing controls.
- **429 or exhausted quota**: routing can try another eligible enabled provider before output begins. Signing in again does not reset Google's quota.

Do not commit downloaded OAuth client files, tokens or private configuration. The repository ignores common credential filenames, but that is not a substitute for checking staged files.
