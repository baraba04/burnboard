// Burnboard: a single window that shows the dashboard.
// No server and no port: the window loads burnboard://app/ through a URL scheme handler, and
// each request runs one short Python process (app/cli.py in the bundle) that ends with it.
// Nothing runs between requests. The page changes settings only through the native bridge
// "usage" (fixed commands, see Bridge), never through a URL.
import Cocoa
import WebKit

let scheme = "burnboard"
let origin = "\(scheme)://app"
let address = URL(string: origin + "/")!
let appDir = Bundle.main.resourceURL!.appendingPathComponent("app")

// The same headers app/serve.py sends: inline code and the bundled d3 only, connections to self only.
let csp = "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
    + "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

// Shown when no Python is found, so it cannot come from cli.py. English only; it has the same two
// buttons as the setup screen and follows the system light and dark colours.
let noPythonPage = """
<!doctype html><meta charset="utf-8"><title>Burnboard</title>
<style>
body { font: 15px/1.5 -apple-system, system-ui, sans-serif; max-width: 560px; margin: 12vh auto; padding: 0 20px; color: #1f1e1d; background: #faf9f5; }
button { font: inherit; font-weight: 500; color: #faf9f5; background: #1f1e1d; border: 0; border-radius: 8px; padding: 8px 16px; cursor: pointer; }
button.alt { color: #1f1e1d; background: transparent; border: 1px solid #dddbd2; }
button:disabled { opacity: .5; }
.row { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
#msg { color: #6b6a68; min-height: 1.5em; }
@media (prefers-color-scheme: dark) {
  body { color: #ecebe6; background: #1c1b19; color-scheme: dark; }
  button { color: #1c1b19; background: #ecebe6; }
  button.alt { color: #ecebe6; border-color: #3a3936; }
  #msg { color: #a3a19a; }
}
</style>
<h2>Python 3 was not found</h2>
<p>Burnboard needs Python 3, which comes with Apple's command line tools. Click the button, confirm in Apple's window and wait for the installation to finish.</p>
<div class="row">
<button id="install" type="button">Install Apple's command line tools</button>
<button id="again" type="button" class="alt">Check again</button>
</div>
<p id="msg" role="status"></p>
<script>
var bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.usage;
var msg = document.getElementById('msg'), install = document.getElementById('install');
install.addEventListener('click', function () {
  if (!bridge) return;
  install.disabled = true;
  bridge.postMessage({ cmd: 'installTools' }).then(function (s) {
    msg.textContent = JSON.parse(s).error
      ? "Apple's installer could not be started. The tools may already be installed: click Check again."
      : "Confirm in Apple's window, wait until it has finished, then click Check again.";
  }).catch(function () { msg.textContent = 'The installer could not be started.'; })
    .finally(function () { install.disabled = false; });
});
document.getElementById('again').addEventListener('click', function () { location.reload(); });
</script>
"""

// One running helper process. cancel() is safe at any time: before the process has started it
// only marks the job, afterwards it sends SIGTERM (cli.py then ends its own helpers).
final class Job {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false

    func start(_ p: Process) -> Bool {
        lock.lock(); defer { lock.unlock() }
        if cancelled { return false }
        do { try p.run() } catch { return false }
        process = p
        return true
    }

    func cancel() {
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        if let p = process, p.isRunning { p.terminate() }
    }
}

final class JobSet {
    private let lock = NSLock()
    private var jobs: [ObjectIdentifier: Job] = [:]
    func add(_ j: Job) { lock.lock(); jobs[ObjectIdentifier(j)] = j; lock.unlock() }
    func remove(_ j: Job) { lock.lock(); jobs[ObjectIdentifier(j)] = nil; lock.unlock() }
    func cancelAll() { lock.lock(); let all = Array(jobs.values); lock.unlock(); all.forEach { $0.cancel() } }
}
let liveJobs = JobSet()

// Without the Xcode command line tools /usr/bin/python3 is a stub that opens an install dialog,
// so it is only started when `xcode-select -p` says the tools are there.
func commandLineToolsPresent() -> Bool {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
    p.arguments = ["-p"]
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    p.standardInput = FileHandle.nullDevice
    do { try p.run() } catch { return false }
    let out = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    guard p.terminationStatus == 0, let path = String(data: out, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) else { return false }
    var isDir: ObjCBool = false
    return FileManager.default.fileExists(atPath: path, isDirectory: &isDir) && isDir.boolValue
}

func findPython() -> String? {
    let fm = FileManager.default
    if fm.isExecutableFile(atPath: "/usr/bin/python3") && commandLineToolsPresent() { return "/usr/bin/python3" }
    return ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"].first { fm.isExecutableFile(atPath: $0) }
}

enum CLIResult {
    case done(Int32, Data)
    case noPython
    case failed
}

// Runs `python3 -I -B cli.py <args>` and waits for it. Call off the main thread.
// -I: isolated (no PYTHON* variables, no user site), -B: no bytecode written into the signed bundle,
// -X utf8: the same text encoding whatever locale the app was started with.
func runCLI(_ args: [String], _ job: Job) -> CLIResult {
    guard let python = findPython() else { return .noPython }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = ["-I", "-B", "-X", "utf8", appDir.appendingPathComponent("cli.py").path] + args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    p.standardInput = FileHandle.nullDevice
    guard job.start(p) else { return .failed }
    let out = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return .done(p.terminationStatus, out)
}

func isProfileID(_ id: String) -> Bool {
    let hex = Set("0123456789abcdef")
    return id == "default" || (id.utf8.count == 8 && id.allSatisfy { hex.contains($0) })
}

// The setup buttons' actions. Each is fixed: nothing from the page reaches an argument, a path or a URL.
// Apple's installer dialog: `xcode-select --install` only opens it and then exits.
func launchToolsInstaller() -> Bool {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
    p.arguments = ["--install"]
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    p.standardInput = FileHandle.nullDevice
    do { try p.run() } catch { return false }
    p.waitUntilExit()
    return p.terminationStatus == 0
}

func openClaudeCodeSite() {
    NSWorkspace.shared.open(URL(string: "https://claude.com/claude-code")!)
}

// The sign-in script goes next to the app's other private files (same folder cli.py uses).
let cacheDir = ProcessInfo.processInfo.environment["USAGE_CLAUDE_CACHE"].flatMap { $0.isEmpty ? nil : $0 }
    ?? NSHomeDirectory() + "/Library/Caches/claude-usage-dashboard"
// Interactive login shell, so a PATH set only in .zshrc (nvm and the like) still finds `claude`.
let signInScript = "#!/bin/zsh -il\nclaude auth login\n"

// Writes the fixed script as 0700 under a fixed name. The bytes go to a new file (exclusive, never
// through a link) that is then renamed over the final name, so a planted link there is replaced, not followed.
func writeSignInScript() -> URL? {
    do {
        try FileManager.default.createDirectory(atPath: cacheDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    } catch { return nil }
    let tmp = cacheDir + "/.sign-in-" + UUID().uuidString
    let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o700)
    guard fd >= 0 else { return nil }
    let bytes = Array(signInScript.utf8)
    let ok = fchmod(fd, 0o700) == 0 && write(fd, bytes, bytes.count) == bytes.count
    close(fd)
    let final = cacheDir + "/sign-in.command"
    guard ok, rename(tmp, final) == 0 else { unlink(tmp); return nil }
    return URL(fileURLWithPath: final)
}

// Claude Code's own browser sign-in through `cli.py sign-in`; waits until it ends (no timer, no polling).
// The reply is cli.py's JSON. When it fails within 5 seconds (the CLI could not start a browser sign-in
// without a terminal) the default profile falls back to the fixed Terminal script; another profile must not,
// because the script would sign in to the default folder.
func runSignIn(_ job: Job) -> (reply: String, script: URL?) {
    let failed = "{\"error\":\"sign_in_failed\"}"
    let started = Date()
    switch runCLI(["sign-in"], job) {
    case .noPython: return ("{\"error\":\"no_python\"}", nil)
    case .failed: return ("{\"error\":\"internal\"}", nil)
    case .done(let status, let out):
        let text = String(data: out, encoding: .utf8) ?? failed
        if status == 0 || Date().timeIntervalSince(started) >= 5 { return (text, nil) }
        guard case .done(0, let kind) = runCLI(["active-is-default"], job),
              String(data: kind, encoding: .utf8) == "{\"default\":true}",
              let script = writeSignInScript() else { return (failed, nil) }
        return (text, script)
    }
}

// Terminal runs the script: opened as a document with the Terminal app, so no Automation permission.
func openInTerminal(_ file: URL, _ done: @escaping (Bool) -> Void) {
    NSWorkspace.shared.open([file], withApplicationAt: URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app"),
                            configuration: NSWorkspace.OpenConfiguration()) { _, error in
        DispatchQueue.main.async { done(error == nil) }
    }
}

// The page's only way to change anything. Accepts messages from the main frame of the app's own
// address, and only these commands; the page never sends a path (profileAdd opens a folder picker).
final class Bridge: NSObject, WKScriptMessageHandlerWithReply {
    weak var window: NSWindow?

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        let o = message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame, o.protocol == scheme, o.host == "app",
              let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else {
            return replyHandler(nil, "refused")
        }
        switch cmd {
        case "consent":
            guard let flag = body["limitsCall"] as? NSNumber, CFGetTypeID(flag) == CFBooleanGetTypeID() else { return replyHandler(nil, "bad message") }
            run(["consent", "--limits-call", flag.boolValue ? "on" : "off"], replyHandler)
        case "profileUse", "profileRemove":
            guard let id = body["id"] as? String, isProfileID(id) else { return replyHandler(nil, "bad message") }
            run([cmd == "profileUse" ? "profile-use" : "profile-remove", "--id", id], replyHandler)
        case "profileAdd":
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.allowsMultipleSelection = false
            panel.showsHiddenFiles = true  // Claude folders start with a dot
            panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser
            panel.message = "Choose a Claude folder (it contains a \"projects\" folder)"
            panel.prompt = "Add"
            let picked: (NSApplication.ModalResponse) -> Void = { response in
                guard response == .OK, let url = panel.url else {
                    return replyHandler("{\"error\":\"cancelled\"}", nil)
                }
                self.run(["profile-add", "--dir", url.path], replyHandler)
            }
            if let w = window { panel.beginSheetModal(for: w, completionHandler: picked) } else { panel.begin(completionHandler: picked) }
        case "installTools", "openClaudeCodeSite", "signIn":
            guard body.count == 1 else { return replyHandler(nil, "bad message") }  // no parameters
            let ok = "{\"ok\":true}", failed = "{\"error\":\"failed\"}"
            switch cmd {
            case "openClaudeCodeSite":
                openClaudeCodeSite()
                replyHandler(ok, nil)
            case "signIn":
                let job = Job()
                liveJobs.add(job)
                DispatchQueue.global(qos: .userInitiated).async {
                    let (reply, script) = runSignIn(job)
                    DispatchQueue.main.async {
                        liveJobs.remove(job)
                        guard let script = script else { return replyHandler(reply, nil) }
                        openInTerminal(script) { replyHandler($0 ? "{\"ok\":true,\"terminal\":true}" : failed, nil) }
                    }
                }
            default:
                DispatchQueue.global(qos: .userInitiated).async {
                    let started = launchToolsInstaller()
                    DispatchQueue.main.async { replyHandler(started ? ok : failed, nil) }
                }
            }
        default:
            replyHandler(nil, "unknown command")
        }
    }

    // The reply is the JSON text cli.py printed.
    private func run(_ args: [String], _ reply: @escaping (Any?, String?) -> Void) {
        let job = Job()
        liveJobs.add(job)
        DispatchQueue.global(qos: .userInitiated).async {
            let result = runCLI(args, job)
            DispatchQueue.main.async {
                liveJobs.remove(job)
                switch result {
                case .done(_, let out): reply(String(data: out, encoding: .utf8) ?? "{\"error\":\"internal\"}", nil)
                case .noPython: reply("{\"error\":\"no_python\"}", nil)
                case .failed: reply("{\"error\":\"internal\"}", nil)
                }
            }
        }
    }
}

// Serves burnboard://app/. Fixed routes, GET only; everything else is a 404.
final class SchemeHandler: NSObject, WKURLSchemeHandler {
    // Touched on the main thread only. A task that the web view has stopped must never be answered.
    private var pending: [ObjectIdentifier: Job] = [:]

    private let routes: [String: (args: [String], type: String, needsHeader: Bool)] = [
        "/": (["page"], "text/html; charset=utf-8", false),
        "/last.json": (["last"], "application/json", true),
        "/data.json": (["data"], "application/json", true),
        "/status.json": (["status"], "application/json", true),
    ]

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        let request = task.request
        guard let url = request.url, url.scheme == scheme, url.host == "app" else { return send(task, 404, "text/plain", "not found") }
        guard request.httpMethod == "GET" else { return send(task, 405, "text/plain", "GET only") }
        if url.path == "/d3.min.js" {
            return start(task, { _ in
                guard let d = try? Data(contentsOf: appDir.appendingPathComponent("vendor/d3.min.js")) else { return (500, "text/plain", Data()) }
                return (200, "text/javascript; charset=utf-8", d)
            })
        }
        guard let route = routes[url.path] else { return send(task, 404, "text/plain", "not found") }
        if route.needsHeader && request.value(forHTTPHeaderField: "X-Usage-Claude") != "1" {
            return send(task, 403, "application/json", "{\"error\":\"forbidden\"}")
        }
        start(task, { job in
            switch runCLI(route.args, job) {
            case .done(let status, let out): return (status == 0 ? 200 : 500, route.type, out)
            case .noPython:
                if url.path == "/" { return (200, "text/html; charset=utf-8", Data(noPythonPage.utf8)) }
                return (500, "application/json", Data("{\"error\":\"no_python\"}".utf8))
            case .failed: return (500, "application/json", Data("{\"error\":\"internal\"}".utf8))
            }
        })
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        if let job = pending.removeValue(forKey: ObjectIdentifier(task)) { job.cancel() }
    }

    // Runs `work` off the main thread; the answer is sent only if the task is still wanted.
    private func start(_ task: WKURLSchemeTask, _ work: @escaping (Job) -> (Int, String, Data)) {
        let job = Job()
        pending[ObjectIdentifier(task)] = job
        liveJobs.add(job)
        DispatchQueue.global(qos: .userInitiated).async {
            let (status, type, body) = work(job)
            DispatchQueue.main.async {
                liveJobs.remove(job)
                guard self.pending.removeValue(forKey: ObjectIdentifier(task)) != nil else { return }
                self.send(task, status, type, body)
            }
        }
    }

    private func send(_ task: WKURLSchemeTask, _ status: Int, _ type: String, _ text: String) {
        send(task, status, type, Data(text.utf8))
    }

    private func send(_ task: WKURLSchemeTask, _ status: Int, _ type: String, _ body: Data) {
        let headers = [
            "Content-Type": type, "Content-Length": String(body.count), "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": csp, "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
        ]
        let response = HTTPURLResponse(url: task.request.url ?? address, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
        task.didReceive(response)
        task.didReceive(body)
        task.didFinish()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var web: WKWebView!
    let bridge = Bridge()

    func applicationDidFinishLaunching(_ note: Notification) {
        let config = WKWebViewConfiguration()
        config.setURLSchemeHandler(SchemeHandler(), forURLScheme: scheme)
        config.userContentController.addScriptMessageHandler(bridge, contentWorld: .page, name: "usage")
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        bridge.window = window
        window.title = "Burnboard"
        window.minSize = NSSize(width: 420, height: 400)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("BurnboardMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        buildMenu()
        web.load(URLRequest(url: address))
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // A running Python process is asked to stop; it then ends its own helpers.
    func applicationWillTerminate(_ note: Notification) { liveJobs.cancelAll() }

    @objc func reloadPage(_ sender: Any?) { web.load(URLRequest(url: address)) }

    // This window only ever shows the app's own page; every attempt to leave it is refused.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        // The page's own "Export CSV": a file it built in memory (blob of this address), saved to Downloads.
        if action.shouldPerformDownload, url.absoluteString.hasPrefix("blob:" + origin + "/") {
            return decisionHandler(.download)
        }
        let local = url.scheme == scheme && url.host == "app"
        if local || url.scheme == "about" {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    var saved: URL?

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        // Keep only a plain file name ending in .csv, never a path the page chose.
        var name = (suggestedFilename as NSString).lastPathComponent
        if name.isEmpty || name.hasPrefix(".") || !name.lowercased().hasSuffix(".csv") { name = "burnboard.csv" }
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        let stem = (name as NSString).deletingPathExtension
        var dest = dir.appendingPathComponent(name)
        var n = 2
        while FileManager.default.fileExists(atPath: dest.path) {
            dest = dir.appendingPathComponent("\(stem) \(n).csv"); n += 1
        }
        saved = dest
        completionHandler(dest)
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let url = saved { try? FileManager.default.removeItem(at: url) }  // no half-written export left behind
        saved = nil
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let url = saved { NSWorkspace.shared.activateFileViewerSelecting([url]) }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let why = error.localizedDescription.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
        let html = "<body style='font:15px -apple-system;padding:40px;color:#888'>The dashboard could not be loaded.<br><br>\(why)<br><br>Press ⌘R to try again.</body>"
        webView.loadHTMLString(html, baseURL: nil)
    }

    // Self-test: USAGE_CLAUDE_SNAPSHOT=/path.png saves a picture of the page and quits.
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let path = ProcessInfo.processInfo.environment["USAGE_CLAUDE_SNAPSHOT"] else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
            webView.takeSnapshot(with: nil) { image, _ in
                if let tiff = image?.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                   let png = rep.representation(using: .png, properties: [:]) {
                    try? png.write(to: URL(fileURLWithPath: path))
                }
                NSApp.terminate(nil)
            }
        }
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Reload Page", action: #selector(reloadPage(_:)), keyEquivalent: "r")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Burnboard", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Burnboard", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = win
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
