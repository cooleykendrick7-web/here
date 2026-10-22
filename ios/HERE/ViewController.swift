import UIKit
import WebKit

/// Hosts the live HERE site in a WKWebView — the iOS twin of the Android
/// shell's MainActivity:
///
/// - Loads https://here-19yl.onrender.com and keeps HERE links in-app.
/// - External web links and tel:/mailto:/sms: open in the system apps.
/// - Back/forward swipe gestures walk the web history (the iOS version
///   of Android's back-button handling).
/// - File uploads just work, with nothing special in this file:
///   WKWebView itself answers <input type="file"> on web pages with the
///   standard iOS picker (Photo Library / Camera / Files) as long as the
///   app uses a plain WKWebView and doesn't block it — which is exactly
///   why this controller stays deliberately standard.
class ViewController: UIViewController, WKNavigationDelegate, WKUIDelegate {

    private static let homeURL = URL(string: "https://here-19yl.onrender.com")!
    private static let homeHost = "here-19yl.onrender.com"

    private var webView: WKWebView!

    override func loadView() {
        let config = WKWebViewConfiguration()
        // Let videos play inline in the page instead of forcing fullscreen,
        // and don't demand a tap before media with sound can start.
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.backgroundColor = UIColor(red: 0.04, green: 0.04, blue: 0.04, alpha: 1) // #0a0a0a
        webView.isOpaque = false
        self.webView = webView
        view = webView
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 0.04, green: 0.04, blue: 0.04, alpha: 1)
        webView.load(URLRequest(url: ViewController.homeURL))
    }

    // MARK: - Navigation policy

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }

        // Links that would open a new window (target="_blank"): decide the
        // same way as a normal tap instead of silently dropping them.
        if navigationAction.targetFrame == nil {
            route(url)
            decisionHandler(.cancel)
            return
        }

        switch url.scheme?.lowercased() {
        case "http", "https":
            if url.host?.lowercased() == ViewController.homeHost {
                decisionHandler(.allow)            // HERE stays in the app
            } else {
                openExternally(url)                // other sites -> Safari
                decisionHandler(.cancel)
            }
        case "tel", "mailto", "sms":
            openExternally(url)                    // phone / mail / messages
            decisionHandler(.cancel)
        case "about", "data", "blob", "javascript":
            decisionHandler(.allow)
        default:
            // Unknown schemes: hand them to the system if it wants them.
            if UIApplication.shared.canOpenURL(url) {
                openExternally(url)
            }
            decisionHandler(.cancel)
        }
    }

    /// Used for target="_blank" links: HERE links load in this web view,
    /// everything else goes to the system.
    private func route(_ url: URL) {
        if url.host?.lowercased() == ViewController.homeHost {
            webView.load(URLRequest(url: url))
        } else if url.scheme == "http" || url.scheme == "https" {
            openExternally(url)
        } else if UIApplication.shared.canOpenURL(url) {
            openExternally(url)
        }
    }

    private func openExternally(_ url: URL) {
        UIApplication.shared.open(url, options: [:], completionHandler: nil)
    }

    // MARK: - New-window requests (target="_blank" inside the page)

    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        // No second web view — route the URL like a normal tap.
        if let url = navigationAction.request.url {
            route(url)
        }
        return nil
    }
}
