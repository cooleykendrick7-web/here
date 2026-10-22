# HERE — Route / Template / DB Contract (BINDING)

Backend (`app.py`) implements EXACTLY these routes. Frontend templates use EXACTLY
these template names, context variable names, and form field names. Do NOT rename
anything without agreement from the coordinator.

Theme: red/black/white everywhere. Primary red `#d41f2e` (buttons, logo, accents,
active states). Black `#111` header. White cards. NO blue anywhere (`#1877f2`
must not appear). Layout follows `ref-layout.html`: fixed black header (red HERE
logo, search input "Search HERE", home/messages/bell icon buttons, avatar),
three-column grid (left nav sidebar, center feed, right contacts sidebar),
responsive: hide right sidebar ≤900px, hide left sidebar ≤600px.

Sessions: signed cookie sessions like `~/workspace/postit/app.py` (sessions table,
token in cookie `here_session`). Read postit's session helpers for the pattern
(get_user, current_user, require_login). DO NOT copy postit's code wholesale —
rebuild it fresh for HERE.

## DB schema (SQLite, tables created in init_db; all created_at = UTC ISO string)

- users(id, name, email UNIQUE, pw_hash, bio DEFAULT '', avatar DEFAULT '', background TEXT DEFAULT '' (relative path under uploads/backgrounds/, e.g. 'backgrounds/abc123.jpg'; '' = default site background), last_seen TEXT, is_verified INTEGER DEFAULT 0, verification_status TEXT DEFAULT 'none' ('none'|'pending'|'approved'|'declined'), created_at)
- sessions(token PRIMARY KEY, user_id, created_at)
- posts(id, user_id, page_id NULL, kind TEXT ('text'|'photo'|'feeling'|'clothes'|'link'|'video'|'audio'), body TEXT DEFAULT '', feeling TEXT DEFAULT '', link_url TEXT DEFAULT '', video_url TEXT DEFAULT '', media TEXT DEFAULT '' (relative upload path), privacy TEXT DEFAULT 'public' ('public'|'friends'|'onlyme'), pinned INTEGER DEFAULT 0, shared_post_id INTEGER NULL (points at original post for share rows), created_at)
- hidden_posts(user_id, post_id, created_at) — UNIQUE(user_id, post_id); hides a post row from that user's feeds only
- post_hidden_from(post_id, user_id, created_at) — UNIQUE(post_id, user_id); per-post custom audience: these users must NOT see the post (enforced everywhere via can_view_post; author + owner context exempt)
- post_tags(id, post_id, user_id NULL, page_id NULL, created_at) — friends/pages tagged in a post; renders "— with …" / tagged page names; tagging never overrides privacy or the hide-from list
- comments(id, post_id, user_id, body, created_at)
- likes(post_id, user_id, kind TEXT DEFAULT 'like' ('like'|'love'|'laugh'|'wow'|'sad'|'angry'), created_at) — UNIQUE(post_id, user_id)
- stories(id, user_id, media (relative upload path), caption DEFAULT '', created_at, expires_at) — 24h TTL, friends + self only
- story_views(story_id, user_id, viewed_at) — UNIQUE(story_id, user_id)
- friendships(id, user_id, friend_id, status TEXT 'pending'|'accepted', created_at) — one row per direction is NOT needed; single row: user_id=requester, friend_id=target. accepted => mutual.
- messages(id, sender_id, receiver_id, body, is_read INTEGER DEFAULT 0, created_at)
- pages(id, owner_id, name, ptype TEXT ('community'|'market'|'fan_club'|'seller'), description DEFAULT '', cover DEFAULT '', is_verified INTEGER DEFAULT 0, verification_status TEXT DEFAULT 'none' ('none'|'pending'|'approved'|'declined'), created_at)
- verification_requests(id, kind TEXT ('user'|'page'), target_id, requester_id, status TEXT 'pending'|'approved'|'declined' DEFAULT 'pending', created_at, reviewed_at TEXT NULL, note TEXT DEFAULT '')
- page_members(page_id, user_id, created_at) — UNIQUE(page_id, user_id); owner auto-member
- taps(id, tapper_id, tapped_id, created_at)
- notifications(id, user_id, kind TEXT ('friend_request'|'tap'|'message'|'share'|'tag'), text, link, is_read INTEGER DEFAULT 0, created_at)
- moderation_flags(id, item_type TEXT ('post'|'comment'|'message'|'story'), item_id INTEGER, author_id, reasons TEXT (comma-joined), excerpt TEXT, status TEXT 'pending'|'dismissed'|'removed' DEFAULT 'pending', created_at)
- owner_sessions(token PRIMARY KEY, created_at)

Upload dirs (under UPLOAD_DIR): avatars/, posts/, pages/, stories/. IMG_EXTS={jpg,jpeg,png,gif,webp},
AUD_EXTS={mp3,wav,ogg,m4a}, MAX_UPLOAD_MB=25. Save with uuid filename. Serve at /uploads.

Owner auth: bcrypt hash embedded in app.py as OWNER_PW_HASH (read the hash string
from /home/hatch/workspace/here/owner_hash.txt and paste it into the code —
it must NOT be read from that file at runtime since that file won't exist on
Render; it is only a build-time source). Owner session cookie: `here_owner`.
Owner login page: GET /owner/login. This route MUST NOT appear in any visible nav;
the only link to it is the tiny yellow "H" on logged-out pages.

Moderation: file `moderation.py` with:
  def scan(text, author_name="") -> tuple[bool, list[str]]
Rules (heuristics, honest): profanity word list, racial slur list, harassment
patterns (e.g. "<name> is a <insult>", "you are a <insult>", "kill yourself",
"nobody likes you", "@name" + insult within same text), slander patterns
("[name] is a liar/thief/scammer/cheater" style accusation without evidence
wording — flag as 'possible slander'), spam (same word 5+ times, 4+ URLs, >80%
caps and length>20), ALL-CAPS shouting. Also:
  PROVIDER = None placeholder + comment: "Set MOD_LLM_HOOK to a callable(text)->reasons to use a real LLM instead of heuristics."
New posts, comments, messages are scanned server-side at creation. If flagged:
insert into moderation_flags, and the item is STILL PUBLISHED (AI only flags,
owner decides).

YouTube/Vimeo embed: helper video_embed_url(url) -> embed src or "".
YouTube: youtu.be/xxx or youtube.com/watch?v=xxx -> https://www.youtube.com/embed/xxx
Vimeo: vimeo.com/12345 -> https://player.vimeo.com/video/12345
Render in template as <iframe>.

last_seen: updated to now on every authenticated page view; online = within 5 min.

## Global template context (every template)
- `user` — dict or None: {id, name, email, bio, avatar}
- `flash` — optional message string
- `pending_count` — incoming friend requests (0 for guests)
- `unread_count` — unread notifications (0 for guests)
- `unread_msgs` — unread messages (0 for guests)
- `page_bg` — full /uploads/ URL of the background to render site-wide, or '': the logged-in viewer's own profile background; profile pages override it with the profile owner's background; '' = default site look

Post card partial `partials/post_card.html`, context var `post` with keys:
  id, user_id, author_name, author_avatar, author_verified (bool — golden ★ beside name when true), kind, body, feeling, link_url,
  video_url, media (full /uploads url or ""), embed_url (first available of:
  video_url embed, link_url embed, first YouTube/Vimeo URL in body; "" if none),
  created_at (display string), likes (count), liked (bool), comments_count,
  can_delete (bool), is_page_post (bool), page_name ("" or name), page_id (0 or id),
  tagged_users [{id,name}], tagged_pages [{id,name}], hidden_from_count (int),
  is_mine (bool), can_edit_audience (bool), body_html (body escaped, then
  bare URLs linkified AND this post's @tags rendered as profile/page links —
  render with |safe; comments carry body_html too, URLs only),
  audience_friends [{id,name,hidden}] (author's friends + current hide-from
  state; only populated on the author's own non-share posts, for the 👁 popup)

Verification: users/pages carry is_verified (bool) and verification_status ('none'|'pending'|'approved'|'declined').
Verified names show a golden five-point star ★ (color #FFD700, white/gold shine + light red glow) beside the name — on profiles, pages, post cards. Verification is owner-only; never auto-approved. Users apply via "⭐ Verify me" on their profile or settings; page owners apply via "⭐ Verify page" on their page.

Profile backgrounds: every user can set a MySpace-style full-page background photo for their profile via Settings ("🖼️ Profile Background"; stored in users.background as 'backgrounds/<uuid>.<ext>', served from /uploads/). The background is rendered full-bleed (cover, centered, scroll attachment for mobile) under a dark gradient scrim, and content cards render translucent frosted glass so text never blends into the photo. It FOLLOWS the logged-in user: their own background is the page background on every logged-in page (via the global `page_bg` context + base.html). Exception: on a profile page, the profile owner's background shows instead. No background set = the default site look, unchanged.

## Routes & templates

| Method | Path | Template | Context / fields |
|---|---|---|---|
| GET | / | redirect /home if logged in else /login | |
| GET | /sw.js | static/sw.js (FileResponse, Service-Worker-Allowed: /) | PWA service worker — caches ONLY manifest + icons; pages/API always network |
| GET | /download/android | static/here.apk (FileResponse) | Real signed Android APK (package com.hereapp.here); 404 if the APK file is absent. Login page "Get the HERE app" card links here; iPhone instead gets Add-to-Home-Screen steps (PWA: /static/manifest.json + /static/icons/*) |
| GET | /signup | auth/signup.html | error? |
| POST | /signup | fields: name, email, password | -> redirect /home |
| GET | /login | auth/login.html | error? — tiny yellow "H" link bottom -> /owner/login |
| POST | /login | fields: email, password | -> redirect /home |
| GET | /logout | | -> redirect /login |
| GET | /home | home.html | posts (post-card dicts, newest first: friends' posts + public posts), contacts (friends: id,name,avatar,online), stories_tray (self + friends' active stories: user_id,name,avatar,is_self,first_id,count,all_viewed) |
| POST | /posts | fields: kind, privacy ('public'|'friends'|'onlyme'), body, feeling, link_url, video_url, media (file), hide_from (friend ids, repeated), tags_json (JSON [{kind:'user'|'page', id, name}] from the composer @ picker — server re-validates: real friend / owned-or-member page AND the literal '@Name' must appear in body), tag_users/tag_pages (legacy checkbox fields, still accepted) | -> redirect /home; tagged friends get a 'tag' notification |
| GET | /api/tag-suggest | ?q= partial name | login required; -> JSON {ok, results: [{kind:'user'|'page', id, name, avatar}]} — current user's friends + pages they own/belong to, case-insensitive substring match on name, friends first, max 8; 401 JSON when logged out |
| GET | /post/{id} | post.html | post (with comments list of {id,user_id,author_name,author_avatar,body,created_at,can_delete}) |
| POST | /post/{id}/comment | field: body | -> redirect /post/{id} |
| POST | /post/{id}/like | | toggle 'like' (removes any existing reaction); -> JSON {ok, likes, liked} (share ids resolve to the original; privacy-gated) |
| POST | /post/{id}/react | field: kind ('like'|'love'|'laugh'|'wow'|'sad'|'angry') | set/change reaction; same kind twice removes it; -> JSON {ok, likes, reacted, kind, top:[{emoji,count}]} |
| GET | /stories/new | story_new.html | story upload form (photo/video + caption) |
| POST | /stories/new | fields: media (file: jpg,png,gif,webp,mp4,mov,webm), caption (max 280) | friends-only, 24h expiry; -> redirect /home |
| GET | /story/{id} | story.html | viewer (author/friends only, unexpired); marks viewed; prev/next; story {id,media,is_video,caption,created_at,pos,total}, author, is_own |
| POST | /story/{id}/delete | | own story or owner; -> redirect /home |
| POST | /post/{id}/delete | | own post or owner; -> redirect back (also deletes its shares) |
| POST | /post/{id}/share | | public posts only; creates share row pointing at original (one per user); -> redirect back |
| POST | /share/{id}/remove | | sharer or owner removes their share row; -> redirect back |
| POST | /post/{id}/hide | | hides the row from YOUR feeds only; -> redirect back |
| POST | /post/{id}/unhide | | -> redirect back |
| GET | /hidden | hidden.html | posts (your hidden posts, with Unhide buttons) |
| POST | /post/{id}/pin | | author only, personal posts; unpins others; -> redirect back |
| POST | /post/{id}/unpin | | author only; -> redirect back |
| GET | /post/{id}/audience | audience.html | author (or owner session) only; friend checkboxes, current hide-from pre-checked; post_id, friends [{id,name,hidden}], hidden_count |
| POST | /post/{id}/audience | field: hide_from (friend ids, repeated) | author (or owner session) only; replaces the post's hide-from list; -> redirect back to the submitting page (feed/wall/post) via same-origin referer, else /post/{id}. The post card's 👁 eyeball popup posts here too |
| GET | /user/{id} | profile.html | profile {id,name,bio,avatar,background (full /uploads/ URL or ''),created_at,friend_count,is_self,req_status ('none'|'pending_out'|'pending_in'|'friends'),tap_count,is_verified,verification_status}, posts, page_bg (= profile.background — the profile owner's background overrides the viewer's site-wide one) |
| POST | /verify/user/request | | logged in; once per user (re-request after decline allowed); sets verification_status='pending' + inserts verification_requests(kind='user'); -> redirect /user/{id} with flash |
| GET | /settings/profile | profile_edit.html | profile |
| POST | /settings/profile | fields: name, bio, avatar (file), background (file: jpg,png,gif,webp, max 5MB -> uploads/backgrounds/), remove_background ('1' clears it) | replaces/deletes old background file; -> redirect /user/{id} |
| GET | /friends | friends.html | incoming [{id,name,avatar,created_at}], sent [{id,name,avatar}], friends [{id,name,avatar,online}] |
| POST | /friends/request/{id} | | -> redirect back |
| POST | /friends/accept/{id} | | -> redirect /friends |
| POST | /friends/decline/{id} | | -> redirect /friends |
| POST | /friends/cancel/{id} | | cancel an outgoing request; -> redirect back |
| POST | /friends/remove/{id} | | -> redirect /friends |
| POST | /tap/{id} | | -> redirect back; creates tap + notification |
| GET | /messages | inbox.html | box ('inbox'|'sent'); threads [{friend_id,friend_name,friend_avatar,last_body,last_at,unread}]; sent [{friend_id,friend_name,friend_avatar,body,created_at}] |
| GET | /messages/{fid} | thread.html | friend {id,name,avatar}, msgs [{id,sender_id,body,created_at,mine}] |
| POST | /messages/{fid} | field: body | -> redirect /messages/{fid} |
| GET | /notifications | notifications.html | notes [{id,kind,text,link,created_at,is_read}] (marks read on view) |
| GET | /discover | discover.html | people [{id,name,avatar,req,views}] = non-friends ranked by profile views received in last 7 days (fallback: newest signups; excludes self/friends/pending), trends [{label,q,count}] = top #hashtags of last 7 days (fallback: most frequent meaningful words) |
| GET | /search | search.html | q (token-split; ANY token matches first/last/display name, case-insensitive partial; exact-phrase ranks first), users [{id,name,avatar,is_self,req}], pages [{id,name,ptype,joined}], posts [post_cards, privacy-respecting] |
| GET | /pages | pages.html | pages [{id,name,ptype,description,cover,members,owner_name,joined}] |
| GET | /pages/new | page_form.html | ptypes list |
| POST | /pages | fields: name, ptype, description, cover (file) | -> redirect /page/{id} |
| GET | /page/{id} | page.html | page {id,name,ptype,description,cover,owner_name,owner_id,members,is_owner,joined,is_verified,verification_status}, posts |
| POST | /verify/page/{id}/request | | page owner only; sets pages.verification_status='pending' + inserts verification_requests(kind='page'); -> redirect /page/{id} with flash |
| POST | /page/{id}/join | | -> redirect /page/{id} |
| POST | /page/{id}/leave | | -> redirect /page/{id} |
| POST | /page/{id}/post | fields: kind, body, feeling, link_url, video_url, media (file), hide_from, tags_json (same @ picker as /posts), tag_users, tag_pages (legacy) | -> redirect /page/{id} |
| GET | /page/{id}/edit | page_edit.html | page (owner only) |
| POST | /page/{id}/edit | fields: name, description, cover (file) | (owner only) -> redirect /page/{id} |
| GET | /owner/login | owner/login.html | error? |
| POST | /owner/login | field: password | -> redirect /owner |
| GET | /owner/logout | | -> redirect /login |
| GET | /owner | owner/dashboard.html | users [{id,name,email,created_at,posts,friends}], friend_activity (recent friendships rows w/ names), flags [{id,item_type,item_id,author_name,excerpt,reasons,status,created_at}], pages [{id,name,ptype,members,posts,interactions}], verifications [{id,kind ('user'|'page'),name,link,requester_name,posts,extra,extra_label,created_at,status}], stats {users, posts_today, msgs_today, taps_today, flags_pending, verify_pending} |
| POST | /owner/verify/{id}/approve | | owner only; sets target is_verified=1 + verification_status='approved', request status='approved' + reviewed_at; -> redirect /owner |
| POST | /owner/verify/{id}/decline | | owner only; sets target verification_status='declined', request status='declined' + reviewed_at; -> redirect /owner |
| POST | /owner/flag/{fid}/dismiss | | -> redirect /owner |
| POST | /owner/flag/{fid}/remove | | deletes the flagged item -> redirect /owner |
| POST | /owner/post/{id}/delete | | -> redirect /owner |
| POST | /owner/user/{id}/remove | | deletes user + their posts/comments/likes/messages -> redirect /owner |

"redirect back": use request.headers.get('referer') or fallback sensible page.

Composer (home + page): a kind selector (Status 📝 | Photo 📷 | Feeling 😊 |
Clothes 👕 | Link 🔗 | Video 🎬 | Music 🎵). Home composer also has an
audience selector: 🌍 Public | 👥 Friends | 🔒 Only me (page composer posts
are always public). Tagging is Facebook-style: typing @ in the body box opens
a dropdown (GET /api/tag-suggest) of friends + owned/member pages filtered by
the typed name; picking one inserts "@Name" and records it in the hidden
tags_json field (client prunes tags whose @Name was deleted; server
re-validates). There are NO tag checkbox pickers anymore; the only checkbox
picker left is "🙈 Hide this post from…" (hide_from). On the author's own
posts, a 👁 eyeball button opens an in-place popup listing their friends
(checked = hidden from this post) that saves via POST /post/{id}/audience. Fields shown per kind via JS:
text: body. photo: body + media(file, images). feeling: body + feeling(select of common
feelings). clothes: body + media(file, images). video: body + media(file,
mp4/mov/webm, max 200MB). audio: body + media(file, audio). There is NO link
kind/box anymore: any http(s) URL pasted in the body renders as a clickable
link, and a YouTube/Vimeo URL in the body auto-embeds under the post. The
link_url/video_url form fields are still accepted server-side (back-compat;
old kind='link'/'video' posts render as before, incl. their 🔗 line/embed).
All POST to /posts (or /page/{id}/post
when on a page, with hidden page context handled by the route itself).

Login/signup pages: dark design, big HERE branding, red accents, email+password
only, and at the very bottom a small yellow single letter "H" linking to
/owner/login. Owner login page: plain, asks for password only, same yellow H
branding.

Header (logged in): red HERE logo -> /home, search input -> GET /search?q=,
icon buttons: 🏠 /home, 💬 /messages (badge unread_msgs), 🔔 /notifications
(badge unread_count), 👥 /friends (badge pending_count), avatar -> /user/{self}.
Left sidebar: My Profile, Friends, Find Friends, Discover, Messages, Pages, Hidden,
Notifications, Logout.
Right sidebar: Discover section (People you may know + Trending this week, only
on /home), then Contacts = friends, online dot for online friends.
Profile views: table profile_views(viewer_id, viewed_id, viewed_at); one counted
view per viewer/viewed pair per hour; self-views never counted. Powers the
Discover rankings.
Avatars: avatar_url() returns '' when the file is missing from disk, so every
avatar <img> falls back to the letter placeholder — no broken-image icons.

All POST forms include no CSRF (keep same pattern as postit). Validate: kinds
in the allowed set; video_url (if sent) must contain youtube/vimeo else reject with flash;
link_url (if sent) must start with http; file ext in allowed sets. Upload caps:
25MB default; post video files (mp4/mov/webm) 200MB, streamed to disk in
chunks by save_upload (never fully in RAM). Uploads are served via the
/uploads StaticFiles mount, which supports HTTP Range (video seek/streaming).

Health check route: GET /healthz -> {"ok": true}.
