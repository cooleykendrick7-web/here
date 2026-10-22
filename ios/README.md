# HERE iPhone app (iOS)

A thin native Swift shell around https://here-19yl.onrender.com — the iOS
twin of the Android app in `~/workspace/here-android`. The whole product is
the website; this app is the installable iPhone package for it.

- Bundle ID: `com.hereapp.here` · version 1.0 · iOS 15+
- Loads the live site in a `WKWebView`, keeps HERE links in-app, sends
  external links and tel:/mailto: to the system, swipe back/forward walks
  history.
- File uploads (photo/video posting) work out of the box: WKWebView
  answers the site's `<input type="file">` with the standard iOS picker
  (Photo Library / Camera / Files). That's why the view controller stays
  deliberately plain — no custom upload code to break it.

## The honest part: signing and installing

The app is built **unsigned**. There is no Apple Developer account
($99/year) behind HERE, and Apple only lets signed apps install — so this
IPA cannot be installed by tapping it in Safari, and it is not on the
App Store.

What does work, free: **sideloading**. The user installs AltStore or
SideStore once (it needs a computer for the first setup), opens
`here.ipa` in it, and signs in with their own Apple ID. The tool re-signs
the app for that one person's phone and installs it. Apps installed this
way must be refreshed by the tool every 7 days (it can do it
automatically over Wi-Fi). The website's iPhone buttons explain exactly
this — no overselling.

If HERE ever gets an Apple Developer account, the same project can be
signed properly and shipped through TestFlight / the App Store instead;
nothing about the app itself has to change.

## How it gets built

Nobody builds this by hand. The website repo is public, so GitHub's
macOS runners do it for free:

- `.github/workflows/build-ipa.yml` runs on every push that touches
  `ios/**` (and on demand from the Actions tab — "Build iOS IPA" →
  Run workflow).
- It builds the app unsigned with `xcodebuild`, packs
  `Payload/HERE.app` into `HERE.ipa`, uploads it as a build artifact,
  and commits it back to `static/here.ipa` on main.
- The live site then serves it at
  https://here-19yl.onrender.com/download/ios — that's the file the
  login page's "Download for iPhone (IPA)" button points at.

## Build it yourself (needs a Mac)

1. Install Xcode from the App Store.
2. Open `HERE.xcodeproj`.
3. In a terminal, from the repo root:

   ```
   xcodebuild build -project ios/HERE.xcodeproj -scheme HERE \
     -configuration Release -destination 'generic/platform=iOS' \
     -derivedDataPath /tmp/here-derived \
     CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY=""
   mkdir -p Payload
   cp -R /tmp/here-derived/Build/Products/Release-iphoneos/HERE.app Payload/
   zip -r HERE.ipa Payload
   ```

4. Install `HERE.ipa` with AltStore / SideStore as described above.

## Layout

```
ios/
  HERE.xcodeproj/          the Xcode project (hand-maintained, opens in Xcode)
  HERE/
    AppDelegate.swift      app entry point
    SceneDelegate.swift    creates the window
    ViewController.swift   the WKWebView shell (all the behavior lives here)
    Info.plist             display name HERE, scene config, orientations
    LaunchScreen.storyboard  black launch screen, red HERE
    Assets.xcassets/       AppIcon — red H on black, generated from
                           static/icons/icon-512.png
```
