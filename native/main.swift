// Burnboard: a single window that shows the dashboard.
// No server and no port: the window loads burnboard://app/ through a URL scheme handler, and
// each request runs one short Python process (app/cli.py in the bundle) that ends with it.
// Nothing runs between requests. The page changes settings only through the native bridge
// "usage" (fixed commands, see Bridge), never through a URL.
import Cocoa
import CryptoKit
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
    // cli.py checks for a newer version only when it knows which one is running (the development server does not).
    if !runningVersion.isEmpty { p.environment = ProcessInfo.processInfo.environment.merging(["BURNBOARD_VERSION": runningVersion]) { $1 } }
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

// --- Update -----------------------------------------------------------------------------------
// The page's Update button sends {"cmd": "update"} and nothing else. Everything is decided here from what
// GitHub serves, never from what the page or Python says: release.json is fetched again, its Ed25519 signature
// is checked against the key built into this app, then the zip is downloaded, its size and SHA-256 are checked,
// it is unpacked and checked as a bundle, and only then does it replace the installed app. A failure at any
// step leaves the installed app as it was and maps to one reply code. Nothing is added to or removed from the
// quarantine attribute.
#if UPDATE_TEST
let testEnvironment = ProcessInfo.processInfo.environment
#endif

let runningVersion: String = {
    #if UPDATE_TEST
    if let v = testEnvironment["BURNBOARD_TEST_VERSION"] { return v }  // the test build is not inside a bundle
    #endif
    return (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? ""
}()

let updatePublicKey: String = {
    #if UPDATE_TEST
    if let k = testEnvironment["BURNBOARD_TEST_PUBKEY"] { return k }  // a throwaway test key, never the real one
    #endif
    return "Hhj8i9whE8GhsyZsktnoQkQrOX5upBaptB4Ktz8f1VE="
}()

let releaseURL: String = {
    #if UPDATE_TEST
    if let u = testEnvironment["BURNBOARD_TEST_RELEASE_URL"] { return u }
    #endif
    return "https://raw.githubusercontent.com/baraba04/burnboard/main/release.json"
}()

let zipURLPrefix: String = {
    #if UPDATE_TEST
    if let u = testEnvironment["BURNBOARD_TEST_ZIP_PREFIX"] { return u }
    #endif
    return "https://github.com/baraba04/burnboard/releases/download/v"
}()

let zipHosts: Set<String> = {
    #if UPDATE_TEST
    return ["127.0.0.1"]
    #else
    return ["github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"]
    #endif
}()

func updateSchemeAllowed(_ url: URL) -> Bool {
    #if UPDATE_TEST
    return url.scheme == "https" || url.scheme == "http"
    #else
    return url.scheme == "https"
    #endif
}

enum UpdateFailure: String, Error {
    case noNetwork = "update_no_network", badSignature = "update_bad_signature", badFile = "update_bad_file"
    case installFailed = "update_install_failed", none = "update_none"
}

let updateMinSize = 100 * 1024, updateMaxSize = 50 * 1024 * 1024
let updateJSONMax = 64 * 1024

// "1.3" or "1.3.1": two or three numbers; anything else is nil.
func versionParts(_ v: String) -> [Int]? {
    let parts = v.split(separator: ".", omittingEmptySubsequences: false)
    guard (2...3).contains(parts.count), v.utf8.count <= 20,
          parts.allSatisfy({ !$0.isEmpty && $0.utf8.allSatisfy { $0 >= 48 && $0 <= 57 } }) else { return nil }
    var numbers = parts.compactMap { Int($0) }
    guard numbers.count == parts.count else { return nil }
    while numbers.count < 3 { numbers.append(0) }
    return numbers
}

func isNewer(_ candidate: [Int], than running: [Int]) -> Bool {
    for (a, b) in zip(candidate, running) where a != b { return a > b }
    return false
}

// The text the signature covers; release-tool.swift builds the same one.
func signedText(_ version: String, _ zip: String, _ sha256: String, _ size: Int) -> Data {
    Data("burnboard-release-v1\n\(version)\n\(zip)\n\(sha256)\n\(size)\n".utf8)
}

struct Release {
    let version: String, zip: String, sha256: String, size: Int
}

// A signed, well-formed release.json whose version is newer than the running one, else the failure.
func checkRelease(_ data: Data) -> Result<Release, UpdateFailure> {
    guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let version = o["version"] as? String, let zip = o["zip"] as? String, let sha = o["sha256"] as? String,
          let sigText = o["sig"] as? String, let sizeNumber = o["size"] as? NSNumber,
          CFGetTypeID(sizeNumber) != CFBooleanGetTypeID(), sizeNumber.doubleValue == Double(sizeNumber.intValue),
          let candidate = versionParts(version),
          zip == zipURLPrefix + version + "/Burnboard-" + version + ".zip",
          (updateMinSize...updateMaxSize).contains(sizeNumber.intValue),
          sha.utf8.count == 64, sha.utf8.allSatisfy({ ($0 >= 48 && $0 <= 57) || ($0 >= 97 && $0 <= 102) || ($0 >= 65 && $0 <= 70) }),
          let sig = Data(base64Encoded: sigText),
          let keyBytes = Data(base64Encoded: updatePublicKey),
          let key = try? Curve25519.Signing.PublicKey(rawRepresentation: keyBytes),
          key.isValidSignature(sig, for: signedText(version, zip, sha, sizeNumber.intValue)) else { return .failure(.badSignature) }
    guard let running = versionParts(runningVersion), isNewer(candidate, than: running) else { return .failure(.none) }
    return .success(Release(version: version, zip: zip, sha256: sha.lowercased(), size: sizeNumber.intValue))
}

// One GET on a fresh ephemeral session (no cookies, no cache). Redirects go only to `hosts` over https. At most
// maxBytes are accepted: more, or any answer other than 200, ends the request. Written to `sink` or kept in `body`.
final class Fetch: NSObject, URLSessionDataDelegate {
    let hosts: Set<String>, maxBytes: Int, sink: FileHandle?
    private(set) var body = Data()
    private(set) var received = 0, tooBig = false, ok = false
    private var hasher = SHA256()
    private var status = 0
    private let finished = DispatchSemaphore(value: 0)

    init(hosts: Set<String>, maxBytes: Int, sink: FileHandle? = nil) {
        self.hosts = hosts; self.maxBytes = maxBytes; self.sink = sink
    }

    var sha256: String { hasher.finalize().map { String(format: "%02x", $0) }.joined() }

    // Blocks until the request has ended or `limit` seconds have passed.
    func get(_ url: URL, limit: TimeInterval) -> Bool {
        guard updateSchemeAllowed(url) else { return false }
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.urlCredentialStorage = nil
        // Nothing about this Mac or this build goes out: fixed values for the two headers URLSession would fill in.
        config.httpAdditionalHeaders = ["User-Agent": "Burnboard-update", "Accept-Language": "en"]
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = limit
        config.timeoutIntervalForResource = limit
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        let session = URLSession(configuration: config, delegate: self, delegateQueue: queue)
        session.dataTask(with: url).resume()
        _ = finished.wait(timeout: .now() + limit)
        session.invalidateAndCancel()
        queue.waitUntilAllOperationsAreFinished()
        return ok
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let url = request.url, updateSchemeAllowed(url), let host = url.host?.lowercased(), hosts.contains(host) else {
            return completionHandler(nil)  // the 3xx answer itself then ends the request as a failure
        }
        completionHandler(request)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 200 && response.expectedContentLength > Int64(maxBytes) { tooBig = true }
        completionHandler(status == 200 && !tooBig ? .allow : .cancel)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        received += data.count
        if received > maxBytes { tooBig = true; return dataTask.cancel() }
        hasher.update(data: data)
        if let sink = sink {
            do { try sink.write(contentsOf: data) } catch { dataTask.cancel(); status = 0 }
        } else {
            body.append(data)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        ok = error == nil && status == 200 && !tooBig
        finished.signal()
    }
}

// Runs a fixed program with an argument list (no shell); its exit status, or -1 when it did not start.
func runTool(_ path: String, _ args: [String]) -> Int32 {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    p.standardInput = FileHandle.nullDevice
    do { try p.run() } catch { return -1 }
    p.waitUntilExit()
    return p.terminationStatus
}

// File type of a path itself (a link is not followed), nil when nothing is there.
func fileKind(_ path: String) -> mode_t? {
    var st = stat()
    return lstat(path, &st) == 0 ? st.st_mode & S_IFMT : nil
}

// The unpacked folder must hold exactly one real Burnboard.app of this bundle id and the signed version, whose
// code signature verifies.
func checkUnpacked(_ folder: URL, _ release: Release) -> URL? {
    guard let items = try? FileManager.default.contentsOfDirectory(atPath: folder.path), items == ["Burnboard.app"] else { return nil }
    let app = folder.appendingPathComponent("Burnboard.app")
    guard fileKind(app.path) == S_IFDIR,
          let info = NSDictionary(contentsOfFile: app.appendingPathComponent("Contents/Info.plist").path),
          info["CFBundleIdentifier"] as? String == "app.burnboard.mac",
          info["CFBundleShortVersionString"] as? String == release.version,
          runTool("/usr/bin/codesign", ["--verify", "--deep", "--strict", app.path]) == 0 else { return nil }
    return app
}

// Where the new app goes, and whether that is this app's own place: the running bundle unless it runs translocated
// or from a folder it cannot write to, else /Applications, else ~/Applications (then `own` is false).
func installTarget() -> (url: URL, own: Bool)? {
    #if UPDATE_TEST
    if let t = testEnvironment["BURNBOARD_TEST_TARGET"] {
        // the test build is not inside a bundle: BURNBOARD_TEST_OWN_PATH names the path that counts as its own place
        return (URL(fileURLWithPath: t), testEnvironment["BURNBOARD_TEST_OWN_PATH"] == t)
    }
    #endif
    let fm = FileManager.default
    let running = Bundle.main.bundleURL
    if running.pathExtension == "app", !running.path.contains("/AppTranslocation/"), fileKind(running.path) == S_IFDIR,
       fm.isWritableFile(atPath: running.deletingLastPathComponent().path) { return (running, true) }
    if fm.isWritableFile(atPath: "/Applications") { return (URL(fileURLWithPath: "/Applications/Burnboard.app"), false) }
    let home = fm.homeDirectoryForCurrentUser.appendingPathComponent("Applications")
    do { try fm.createDirectory(at: home, withIntermediateDirectories: true) } catch { return nil }
    return (home.appendingPathComponent("Burnboard.app"), false)
}

// Something already at a fallback target (not this app's own place) is replaced only when it is a real bundle
// folder of this app; a link, a file or another app's bundle is left alone.
func isBurnboardBundle(_ path: String) -> Bool {
    return bundleInfo(path)?["CFBundleIdentifier"] as? String == "app.burnboard.mac"
}

// Info.plist of a bundle folder, read only when the folder, its Contents and the file are real (no link) and the
// file is small: a planted bundle must not make the app read through a link or load a huge file.
func bundleInfo(_ path: String) -> NSDictionary? {
    let plist = path + "/Contents/Info.plist"
    var st = stat()
    guard fileKind(path) == S_IFDIR, fileKind(path + "/Contents") == S_IFDIR,
          lstat(plist, &st) == 0, st.st_mode & S_IFMT == S_IFREG, st.st_size <= 1024 * 1024 else { return nil }
    return NSDictionary(contentsOfFile: plist)
}

let stagedPrefix = ".Burnboard-new-", asidePrefix = ".Burnboard-old-"

// What an interrupted earlier update left next to the target: only the names install() makes (prefix and a UUID),
// removed as themselves (a link is unlinked, never followed). An aside folder is the old app, so it goes only
// when the target exists again.
func removeLeftovers(in parent: String, targetExists: Bool) {
    let fm = FileManager.default
    guard let names = try? fm.contentsOfDirectory(atPath: parent) else { return }
    for name in names {
        let isStaged = name.hasPrefix(stagedPrefix) && UUID(uuidString: String(name.dropFirst(stagedPrefix.count))) != nil
        let isAside = name.hasPrefix(asidePrefix) && UUID(uuidString: String(name.dropFirst(asidePrefix.count))) != nil
        if isStaged || (isAside && targetExists) { try? fm.removeItem(atPath: parent + "/" + name) }
    }
}

// rename() that never replaces what is at `to`: renamex_np with RENAME_EXCL, plain rename() (after a check) on a
// volume that does not support it.
func moveNoReplace(_ from: String, _ to: String) -> Bool {
    if renamex_np(from, to, UInt32(RENAME_EXCL)) == 0 { return true }
    guard errno == ENOTSUP || errno == EINVAL, fileKind(to) == nil else { return false }
    return rename(from, to) == 0
}

// The swapped-out old bundle: to the Trash; if that fails, removed; if that fails too, left in place. First it gets a
// visible name, so that what stays behind (or sits in the Trash) is findable and not a hidden dot folder.
func disposeOld(_ path: String, in parent: String, id: String) {
    let fm = FileManager.default
    var current = path
    let visible = parent + "/Burnboard (previous " + id.prefix(8) + ").app"
    if moveNoReplace(path, visible) { current = visible }
    let url = URL(fileURLWithPath: current)
    #if UPDATE_TEST
    // never the real Trash: BURNBOARD_TEST_TRASH is a folder to move into, "fail" (or unset) makes the Trash step fail
    if let dest = testEnvironment["BURNBOARD_TEST_TRASH"], dest != "fail",
       (try? fm.moveItem(at: url, to: URL(fileURLWithPath: dest).appendingPathComponent(url.lastPathComponent))) != nil { return }
    #else
    if (try? fm.trashItem(at: url, resultingItemURL: nil)) != nil { return }
    #endif
    try? fm.removeItem(at: url)  // when this fails too the old app stays under its visible name; the new one is in place
}

// Puts the verified bundle at the target without a moment where no app is there. It is copied next to the target
// (same volume) and checked again; an existing target is exchanged with it by renamex_np(RENAME_SWAP), so the old
// bundle ends up at the staged name and goes through disposeOld. A volume without exchange support (ENOTSUP,
// EINVAL) takes the two-rename way: old aside, new in, old back when that fails. These calls act on the link
// itself, never on where it points: a link at the target is exchanged or moved aside like a bundle would be.
func install(_ newApp: URL, to target: URL, own: Bool) -> Bool {
    let fm = FileManager.default
    let parent = target.deletingLastPathComponent().path
    let hadOld = fileKind(target.path) != nil
    if !own && hadOld && !isBurnboardBundle(target.path) { return false }  // touch nothing
    removeLeftovers(in: parent, targetExists: hadOld)
    let id = UUID().uuidString
    let staged = parent + "/" + stagedPrefix + id, old = parent + "/" + asidePrefix + id
    guard runTool("/usr/bin/ditto", [newApp.path, staged]) == 0,
          runTool("/usr/bin/codesign", ["--verify", "--deep", "--strict", staged]) == 0 else {
        try? fm.removeItem(atPath: staged)
        return false
    }
    var unsupported = false
    #if UPDATE_TEST
    unsupported = testEnvironment["BURNBOARD_TEST_NOSWAP"] != nil  // models a volume without exchange support
    #endif
    if !unsupported {
        if renamex_np(staged, target.path, UInt32(hadOld ? RENAME_SWAP : RENAME_EXCL)) == 0 {
            if hadOld { disposeOld(staged, in: parent, id: id) }
            return true
        }
        unsupported = errno == ENOTSUP || errno == EINVAL
    }
    guard unsupported else { try? fm.removeItem(atPath: staged); return false }

    if hadOld && rename(target.path, old) != 0 { try? fm.removeItem(atPath: staged); return false }
    var placed = rename(staged, target.path) == 0
    #if UPDATE_TEST
    if testEnvironment["BURNBOARD_TEST_NOSWAP"] == "2" {  // models a failure of this rename
        if placed { _ = rename(target.path, staged) }
        placed = false
    }
    #endif
    guard placed else {
        try? fm.removeItem(atPath: staged)
        // put the old one back; when even that fails, it stays in this folder under a visible name
        if hadOld && !moveNoReplace(old, target.path) { _ = moveNoReplace(old, parent + "/Burnboard (previous " + id.prefix(8) + ").app") }
        return false
    }
    if hadOld { disposeOld(old, in: parent, id: id) }
    return true
}

// Quit gate. Between "download verified" and "replace finished" the app must not terminate (a quit in the middle
// of the rename steps would leave a half-done install). While `busy`, applicationShouldTerminate answers
// .terminateLater; end() then answers the held request. Quit by menu, Cmd-Q and the last window closing all end in
// NSApp.terminate, so they all pass through applicationShouldTerminate. Touched from two threads: under the lock.
final class QuitGate {
    private let lock = NSLock()
    private var busy = false, held = false

    func begin() { lock.lock(); busy = true; lock.unlock() }

    // True when a quit request has to wait (and will be answered by end()).
    func shouldHold() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if busy { held = true }
        return busy
    }

    func end() {
        lock.lock()
        busy = false
        let answer = held
        held = false
        lock.unlock()
        if answer { DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) } }
    }
}
let quitGate = QuitGate()

// All steps of the update, on the calling (background) thread. Returns where the new app is installed.
func runUpdate() -> Result<URL, UpdateFailure> {
    guard let url = URL(string: releaseURL), let host = url.host?.lowercased() else { return .failure(.noNetwork) }
    let listing = Fetch(hosts: [host], maxBytes: updateJSONMax)
    guard listing.get(url, limit: 15) else { return .failure(.noNetwork) }
    let release: Release
    switch checkRelease(listing.body) {
    case .failure(let why): return .failure(why)
    case .success(let r): release = r
    }
    guard let zipURL = URL(string: release.zip) else { return .failure(.badSignature) }

    var template = Array((NSTemporaryDirectory() + "burnboard-update-XXXXXX").utf8CString)
    guard mkdtemp(&template) != nil else { return .failure(.installFailed) }  // 0700
    let work = URL(fileURLWithPath: String(cString: template))
    var gated = false
    defer { if gated { quitGate.end() } }  // declared first, so it runs after the temporary folder is gone
    defer { try? FileManager.default.removeItem(at: work) }

    let zipPath = work.appendingPathComponent("Burnboard.zip").path
    let fd = open(zipPath, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { return .failure(.installFailed) }
    let sink = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    let download = Fetch(hosts: zipHosts, maxBytes: release.size, sink: sink)
    let fetched = download.get(zipURL, limit: 120)
    try? sink.close()
    if download.tooBig { return .failure(.badFile) }
    guard fetched else { return .failure(.noNetwork) }
    guard download.received == release.size, download.sha256 == release.sha256 else { return .failure(.badFile) }
    gated = true
    quitGate.begin()  // from here to the end of install() the app does not quit

    let unpacked = work.appendingPathComponent("unpacked")
    guard (try? FileManager.default.createDirectory(at: unpacked, withIntermediateDirectories: false,
                                                    attributes: [.posixPermissions: 0o700])) != nil,
          runTool("/usr/bin/ditto", ["-x", "-k", zipPath, unpacked.path]) == 0,
          let newApp = checkUnpacked(unpacked, release) else { return .failure(.badFile) }

    guard let target = installTarget(), install(newApp, to: target.url, own: target.own) else { return .failure(.installFailed) }
    return .success(target.url)
}

// Updates run one at a time (updateInstalled is touched on updateQueue only); once one has installed, a repeated
// click only gets "ok" and the restart is scheduled once (restarting is touched on the main thread only).
let updateQueue = DispatchQueue(label: "burnboard.update", qos: .userInitiated)
var updateInstalled: URL?
var restarting = false

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
        case "update":
            guard body.count == 1 else { return replyHandler(nil, "bad message") }  // no parameters
            updateQueue.async {
                let result = updateInstalled.map { Result<URL, UpdateFailure>.success($0) } ?? runUpdate()
                if case .success(let target) = result { updateInstalled = target }
                DispatchQueue.main.async {
                    switch result {
                    case .failure(let why): replyHandler("{\"error\":\"\(why.rawValue)\"}", nil)
                    case .success(let target):
                        replyHandler("{\"ok\":true}", nil)
                        guard !restarting else { return }
                        restarting = true
                        // Start the new copy once the reply has reached the page, then end this one.
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                            _ = runTool("/usr/bin/open", ["-n", target.path])
                            NSApp.terminate(nil)
                        }
                    }
                }
            }
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
            case .done(let status, let out):
                return (status == 0 ? 200 : 500, route.type, out)
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

    // Quitting waits while an update is replacing the app (see QuitGate); otherwise it quits as always.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        quitGate.shouldHold() ? .terminateLater : .terminateNow
    }

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

#if UPDATE_TEST
// Test builds only: `Burnboard --update-test` runs the update steps without a window and prints the reply
// (tests/test_update.py). No relaunch.
if CommandLine.arguments.dropFirst().first == "--update-test" {
    switch runUpdate() {
    case .success: print("{\"ok\":true}")
    case .failure(let why): print("{\"error\":\"\(why.rawValue)\"}")
    }
    exit(0)
}
#endif

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
