# Outlook access: what Node can and cannot get

## Does it need an app approval?

**Not a new app registration or a Thunderbird/MailCraft client ID.** The 0.16
Outlook connector uses the signed-in Outlook Web session already held by Node's
browser profile. It asks Outlook Web to make a normal request, copies that
request's bearer header in memory, then replays the same request as HTTP. This
is not an attempt to impersonate Thunderbird, spoof a client ID, or bypass an
admin decision about third-party apps. It reuses the account's own Outlook Web
authorization.

There are two different policy gates:

1. **Sign-in to Outlook Web itself.** If the school blocks the account from
   Outlook Web, or requires an action the server's browser cannot complete,
   the connector cannot mint a session.
2. **Access for third-party clients/apps.** This is what blocked MailCraft or
   Graph app consent. The OWA connector does not request that third-party app
   consent. It can still be stopped by tenant policy, Conditional Access,
   session expiry, or Microsoft changing the private OWA endpoints.

A successful token mint only proves OWA signed in and emitted a bearer token;
it does not prove the endpoint will accept every operation. Node should only
read data the signed-in account can read. We should not spoof another mail
client's identity or reuse a client id/refresh token intended for another app.

## What the current connector pulls

As of v0.16.0, `outlook-mail`:

- asks OWA for the **Inbox conversation list**, newest first, currently up to
  50 conversations per poll (matching the captured OWA paging size);
- imports the topic, preview, last sender name, last-delivery time, unread
  state, message count, attachment-present flag, importance and a link back to
  Outlook;
- fetches the newest message's **unique body text** for up to 8 new unread
  conversations in a run (bounded by Node's configured body length);
- updates an existing Inbox row when a new version/reply arrives in the same
  conversation; marks it read in Node when OWA says the conversation is read.

It does **not** currently download attachment bytes, import all old/read mail,
scan Sent/Junk/Archive/custom folders, fetch a complete thread history, send or
move mail, mark mail read in Outlook, sync contacts, or sync calendars. Those
are separate features, not hidden capabilities of the current sync.

## What else the web API exposes (capture-confirmed)

The user's `record.py` capture documented these OWA service calls:

- `FindConversation`: conversation lists (used now).
- `GetConversationItems`: thread messages and unique bodies (used now, newest
  message only).
- `GetFolder` / `GetFolderChangeDigest`: folder metadata and a cheap inbox
  change/unread check (captured but not integrated; exact folder/watermark
  request needs a sanitized capture before relying on it).
- `FindItem`: drafts (captured; not integrated).
- `GetItem`: single-item details (captured; not integrated).
- `GetAttachmentPreviews`: attachment metadata/previews (captured; no file
  download implemented).
- `ApplyConversationAction`: Outlook actions including marking a conversation
  read (captured; deliberately no write-back yet).
- `GetCalendarView`: calendar view (captured; not integrated).

Some OWA deployments also expose REST-style paths such as `/api/v2.0/me/...`.
These are not yet verified against this user's account. Do not assume an
endpoint works just because a community client uses it.

## Open-source implementation leads

These are leads for endpoint behavior, not endorsements and not credentials to
reuse:

- [weirdapps/outlook-access](https://github.com/weirdapps/outlook-access) is a
  current TypeScript CLI that captures the user's own Outlook Web session and
  uses it for mail, folders, calendar and attachment operations. Its public
  source is MIT. This confirms the general “signed-in OWA session, no separate
  app registration” approach and is a useful reference for endpoint shapes.
  Node currently implements its own small, read-only subset rather than
  importing this CLI or its runtime.
- [ghostwriteai/outlookjs](https://github.com/ghostwriteai/outlookjs) hooks an
  open Outlook Web page. It is useful for event observation while a page is
  open, not a server-side background protocol.
- IMAP libraries are not an authentication bypass. Microsoft 365 IMAP uses
  OAuth/XOAUTH2 where enabled; a client still needs a permitted token, and a
  school can disable IMAP or block the OAuth client. Outlook's web session
  bearer token must not be assumed valid for IMAP (different audience).

## A safe expansion order

1. **Next, evidence first:** ask for a sanitized capture of one `GetFolder` and
   `GetFolderChangeDigest` request/response, plus one message with an
   attachment. Add folder polling and attachment metadata only after tests can
   assert the exact contract.
2. Add optional folder selection (Inbox first; Sent/Archive/Junk only by
   explicit setting) and pagination with stable ids. Avoid silently flooding
   the Inbox with a whole mailbox.
3. Add attachment download behind a size limit and an explicit storage policy;
   store files only under the ignored data directory, never in Git.
4. Add calendar import as a separate connector into the existing calendar
   ingestion path, after confirming the OWA calendar response and dedupe key.
5. Keep writes (mark-read, move, delete, send) off by default and require an
   explicit user action. Read-only first.

**Reliability boundary:** OWA's internal service endpoints are undocumented and
can change without notice. A protocol implementation (IMAP) is more standard,
but only works when the account and OAuth policy permit it. For this account,
OWA session replay is currently the practical route; no alternate client id is
needed, and no third-party app approval should be requested by this connector.
