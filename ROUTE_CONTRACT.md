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

- users(id, name, email UNIQUE, pw_hash, bio DEFAULT '', avatar DEFAULT '', last_seen TEXT, created_at)
- sessions(token PRIMARY KEY, user_id, created_at)
- posts(id, user_id, page_id NULL, kind TEXT ('text'|'photo'|'feeling'|'clothes'|'link'|'video'|'audio'), body TEXT DEFAULT '', feeling TEXT DEFAULT '', link_url TEXT DEFAULT '', video_url TEXT DEFAULT '', media TEXT DEFAULT '' (relative upload path), privacy TEXT DEFAULT 'public' ('public'|'friends'|'onlyme'), pinned INTEGER DEFAULT 0, shared_post_id INTEGER NULL (points at original post for share rows), created_at)
- hidden_posts(user_id, post_id, created_at) — UNIQUE(user_id, post_id); hides a post row from that user's feeds only
- comments(id, post_id, user_id, body, created_at)
- likes(post_id, user_id, created_at) — UNIQUE(post_id, user_id)
- friendships(id, user_id, friend_id, status TEXT 'pending'|'accepted', created_at) — one row per direction is NOT needed; single row: user_id=requester, friend_id=target. accepted => mutual.
- messages(id, sender_id, receiver_id, body, is_read INTEGER DEFAULT 0, created_at)
- pages(id, owner_id, name, ptype TEXT ('community'|'market'|'fan_club'|'seller'), description DEFAULT '', cover DEFAULT '', created_at)
- page_members(page_id, user_id, created_at) — UNIQUE(page_id, user_id); owner auto-member
- taps(id, tapper_id, tapped_id, created_at)
- notifications(id, user_id, kind TEXT ('friend_request'|'tap'|'message'), text, link, is_read INTEGER DEFAULT 0, created_at)
- moderation_flags(id, item_type TEXT ('post'|'comment'|'message'), item_id INTEGER, author_id, reasons TEXT (comma-joined), excerpt TEXT, status TEXT 'pending'|'dismissed'|'removed' DEFAULT 'pending', created_at)
- owner_sessions(token PRIMARY KEY, created_at)

Upload dirs (under UPLOAD_DIR): avatars/, posts/, pages/. IMG_EXTS={jpg,jpeg,png,gif,webp},
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

Post card partial `partials/post_card.html`, context var `post` with keys:
  id, user_id, author_name, author_avatar, kind, body, feeling, link_url,
  video_url, media (full /uploads url or ""), embed_url (for video kind, "" if none),
  created_at (display string), likes (count), liked (bool), comments_count,
  can_delete (bool), is_page_post (bool), page_name ("" or name), page_id (0 or id)

## Routes & templates

| Method | Path | Template | Context / fields |
|---|---|---|---|
| GET | / | redirect /home if logged in else /login | |
| GET | /signup | auth/signup.html | error? |
| POST | /signup | fields: name, email, password | -> redirect /home |
| GET | /login | auth/login.html | error? — tiny yellow "H" link bottom -> /owner/login |
| POST | /login | fields: email, password | -> redirect /home |
| GET | /logout | | -> redirect /login |
| GET | /home | home.html | posts (post-card dicts, newest first: friends' posts + public posts), contacts (friends: id,name,avatar,online) |
| POST | /posts | fields: kind, privacy ('public'|'friends'|'onlyme'), body, feeling, link_url, video_url, media (file) | -> redirect /home |
| GET | /post/{id} | post.html | post (with comments list of {id,user_id,author_name,author_avatar,body,created_at,can_delete}) |
| POST | /post/{id}/comment | field: body | -> redirect /post/{id} |
| POST | /post/{id}/like | | toggle; -> JSON {ok, likes, liked} (share ids resolve to the original; privacy-gated) |
| POST | /post/{id}/delete | | own post or owner; -> redirect back (also deletes its shares) |
| POST | /post/{id}/share | | public posts only; creates share row pointing at original (one per user); -> redirect back |
| POST | /share/{id}/remove | | sharer or owner removes their share row; -> redirect back |
| POST | /post/{id}/hide | | hides the row from YOUR feeds only; -> redirect back |
| POST | /post/{id}/unhide | | -> redirect back |
| GET | /hidden | hidden.html | posts (your hidden posts, with Unhide buttons) |
| POST | /post/{id}/pin | | author only, personal posts; unpins others; -> redirect back |
| POST | /post/{id}/unpin | | author only; -> redirect back |
| GET | /user/{id} | profile.html | profile {id,name,bio,avatar,created_at,friend_count,is_self,req_status ('none'|'pending_out'|'pending_in'|'friends'),tap_count}, posts |
| GET | /settings/profile | profile_edit.html | profile |
| POST | /settings/profile | fields: name, bio, avatar (file) | -> redirect /user/{id} |
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
| GET | /search | search.html | q, users [{id,name,avatar,is_self,req}], pages [{id,name,ptype}] |
| GET | /pages | pages.html | pages [{id,name,ptype,description,cover,members,owner_name,joined}] |
| GET | /pages/new | page_form.html | ptypes list |
| POST | /pages | fields: name, ptype, description, cover (file) | -> redirect /page/{id} |
| GET | /page/{id} | page.html | page {id,name,ptype,description,cover,owner_name,owner_id,members,is_owner,joined}, posts |
| POST | /page/{id}/join | | -> redirect /page/{id} |
| POST | /page/{id}/leave | | -> redirect /page/{id} |
| POST | /page/{id}/post | fields: kind, body, feeling, link_url, video_url, media (file) | -> redirect /page/{id} |
| GET | /page/{id}/edit | page_edit.html | page (owner only) |
| POST | /page/{id}/edit | fields: name, description, cover (file) | (owner only) -> redirect /page/{id} |
| GET | /owner/login | owner/login.html | error? |
| POST | /owner/login | field: password | -> redirect /owner |
| GET | /owner/logout | | -> redirect /login |
| GET | /owner | owner/dashboard.html | users [{id,name,email,created_at,posts,friends}], friend_activity (recent friendships rows w/ names), flags [{id,item_type,item_id,author_name,excerpt,reasons,status,created_at}], pages [{id,name,ptype,members,posts,interactions}], stats {users, posts_today, msgs_today, taps_today, flags_pending} |
| POST | /owner/flag/{fid}/dismiss | | -> redirect /owner |
| POST | /owner/flag/{fid}/remove | | deletes the flagged item -> redirect /owner |
| POST | /owner/post/{id}/delete | | -> redirect /owner |
| POST | /owner/user/{id}/remove | | deletes user + their posts/comments/likes/messages -> redirect /owner |

"redirect back": use request.headers.get('referer') or fallback sensible page.

Composer (home + page): a kind selector (Status 📝 | Photo 📷 | Feeling 😊 |
Clothes 👕 | Link 🔗 | Video 🎬 | Music 🎵). Home composer also has an
audience selector: 🌍 Public | 👥 Friends | 🔒 Only me (page composer posts
are always public). Fields shown per kind via JS:
text: body. photo: body + media(file). feeling: body + feeling(select of common
feelings). clothes: body + media(file). link: body + link_url. video: body +
video_url. audio: body + media(file .mp3). All POST to /posts (or /page/{id}/post
when on a page, with hidden page context handled by the route itself).

Login/signup pages: dark design, big HERE branding, red accents, email+password
only, and at the very bottom a small yellow single letter "H" linking to
/owner/login. Owner login page: plain, asks for password only, same yellow H
branding.

Header (logged in): red HERE logo -> /home, search input -> GET /search?q=,
icon buttons: 🏠 /home, 💬 /messages (badge unread_msgs), 🔔 /notifications
(badge unread_count), 👥 /friends (badge pending_count), avatar -> /user/{self}.
Left sidebar: My Profile, Friends, Messages, Pages, Notifications, Logout.
Right sidebar: Contacts = friends, online dot for online friends.

All POST forms include no CSRF (keep same pattern as postit). Validate: kinds
in the allowed set; video_url must contain youtube/vimeo else reject with flash;
link_url must start with http; file ext in allowed sets.

Health check route: GET /healthz -> {"ok": true}.
