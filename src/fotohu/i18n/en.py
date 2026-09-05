"""English fallback texts."""

TEXTS: dict[str, str] = {
    "start.unknown": (
        "👋 This is a family photo archiver.\n\n"
        "You are not on the member list yet. Ask the admin for an invite code and "
        "send it as:\n<code>/join CODE</code>"
    ),
    "start.known": (
        "👋 Hi {name}!\n\n"
        "Send photos <b>as files</b> — I will put them in the cloud at original "
        "quality and clear them from the chat.\n\n"
        "📁 Your folder: <code>{folder}</code>\n"
        "❓ How to send uncompressed — /howto"
    ),
    "start.bootstrap": (
        "🔑 You are now the admin of this bot.\n\n"
        "Next: connect a cloud and create invites for the family — /admin"
    ),
    "join.ok": "✅ Welcome, {name}!\n📁 Your folder: <code>{folder}</code>",
    "join.bad_code": "❌ That code did not work: unknown, already used, revoked or expired.",
    "join.already": "You are already registered 🙂",
    "join.usage": "Usage: <code>/join CODE</code>",
    "access.denied": (
        "🚫 Members only.\n"
        "If you should have access, ask for a code and send <code>/join CODE</code>."
    ),
    "access.blocked": "🚫 Your access has been suspended by the admin.",
    "access.admin_only": "🚫 Admins only.",

    "upload.queued": "📥 Got “{name}”, uploading…",
    "upload.ok": "✅ Saved: <code>{path}</code>",
    "upload.ok_unverified": (
        "✅ Saved: <code>{path}</code>\n"
        "⚠️ The provider returned no checksum — size verified, byte-level check unavailable."
    ),
    "upload.duplicate": "♻️ Already archived: <code>{path}</code>",
    "quality.rejected": (
        "⚠️ That is a compressed copy — the original is still on your phone.\n\n"
        "The messenger re-encodes photos <b>on its own servers</b>, so the original "
        "cannot be recovered. Send it <b>as a file</b> and I will store it losslessly.\n\n"
        "How — /howto"
    ),
    "quality.marked": "✅ Saved as a compressed copy: <code>{path}</code>",

    "err.no_storage": "⚠️ No cloud connected yet. Admin: /admin → Storage.",
    "err.too_large": "❌ The file is over {mb} MB — the messenger will not hand it over.",
    "err.telegram_20mb": (
        "❌ Telegram does not give bots files larger than 20 MB.\n"
        "The admin can run a self-hosted Bot API server (the <code>local-api</code> "
        "profile) to raise this to 2 GB."
    ),
    "err.storage_auth": "🔐 The cloud rejected our credentials: {error}\nRe-link it via /admin",
    "err.quota": "💾 The cloud is out of space. Uploads are paused.",
    "err.integrity": (
        "🛑 Checksum mismatch: {error}\n"
        "The chat copy was NOT deleted — it may be the only intact one."
    ),
    "err.generic": "❌ Failed: {error}",

    "howto": (
        "<b>Sending photos without quality loss</b>\n\n"
        "Messengers re-compress photos and strip EXIF. Sending as a file is the only "
        "way the exact bytes reach the cloud.\n\n"
        "📱 <b>Telegram, Android</b>\n"
        "Paperclip → Gallery → pick photo → ⋮ → “Send without compression”.\n\n"
        "📱 <b>Telegram, iPhone</b>\n"
        "Paperclip → File → “Photos” → pick it. Or long-press Send →\n"
        "“Send without compression”.\n\n"
        "💻 <b>Telegram Desktop</b>\n"
        "Drag the file in and untick “Compress images”.\n\n"
        "📱 <b>Viber</b>\n"
        "Paperclip → “File” (not “Gallery”) → pick the photo.\n\n"
        "💡 You can send several at once as an album — I unpack them one by one."
    ),
    "help.member": (
        "<b>Commands</b>\n"
        "/me — my folder and stats\n"
        "/last — recent uploads\n"
        "/howto — how to send uncompressed\n"
        "/help — this help"
    ),
    "help.admin_extra": "\n/admin — admin panel",

    "me": (
        "👤 <b>{name}</b>\n"
        "Role: {role}\n"
        "📁 Folder: <code>{folder}</code>\n"
        "🖼 Archived: {count} ({size})"
    ),
    "album.queued": "📥 Got the album ({n} file(s)), uploading…",
    "album.done": "✅ Album: {n} saved.",
    "album.duplicates": "♻️ Already archived: {n}",
    "album.rejected": "⚠️ Compressed, skipped: {n} (send as files — /howto)",
    "album.failed": "❌ Failed: {n} (will retry automatically)",

    "last.empty": "Nothing uploaded yet.",
    "last.header": "<b>Recent uploads</b>\n",

    "admin.new_upload": "📸 <b>{name}</b> uploaded a photo:\n<code>{path}</code>",
    "admin.new_album": "📸 <b>{name}</b> uploaded an album: {n} photo(s).",
}
