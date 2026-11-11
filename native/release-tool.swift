// Release tool of Burnboard: signs the release description (release.json) that the app's updater checks.
// The private key is one line in the project's .env (BURNBOARD_UPDATE_KEY=<base64 raw key>); it is never
// printed, never a process argument and never written anywhere else.
//   swiftc -O -o release-tool native/release-tool.swift   (package.sh does this), then
//   release-tool keygen                       new key into .env (refuses when one exists); prints the public key
//   release-tool pubkey                       prints the public key of the stored key
//   release-tool sign --zip PATH --version V [--out PATH]   writes dist/release.json (or PATH)
//   release-tool verify --json PATH --zip PATH [--pubkey B64]   checks a release.json and its zip, against the stored
//                                             key, or against the given public key (package.sh passes the app's constant)
//   release-tool app-pubkey [--main PATH]     prints the updatePublicKey constant of native/main.swift
//   release-tool check-key [--main PATH]      fails unless the public half of the stored key is that constant
// The signed text is the same one main.swift checks (signedText there).
// BURNBOARD_ENV_FILE moves the .env path (the tests use it); it never carries the key itself.
import CryptoKit
import Foundation

let keyName = "BURNBOARD_UPDATE_KEY"
let zipPrefix = "https://github.com/baraba04/burnboard/releases/download/v"
let minSize = 100 * 1024, maxSize = 50 * 1024 * 1024

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func option(_ name: String) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: name), i + 1 < a.count else { return nil }
    return a[i + 1]
}

func envPath() -> String {
    if let p = ProcessInfo.processInfo.environment["BURNBOARD_ENV_FILE"], !p.isEmpty { return p }
    return URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".env").path
}

func keyLine(_ path: String) -> String? {
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
    return text.split(separator: "\n", omittingEmptySubsequences: true).first { $0.hasPrefix(keyName + "=") }.map(String.init)
}

func loadKey() -> Curve25519.Signing.PrivateKey {
    let path = envPath()
    guard let line = keyLine(path) else { fail("no \(keyName) line in \(path): run keygen first") }
    let value = line.dropFirst(keyName.count + 1).trimmingCharacters(in: .whitespaces)
    guard let raw = Data(base64Encoded: value), let key = try? Curve25519.Signing.PrivateKey(rawRepresentation: raw) else {
        fail("the \(keyName) line in \(path) is not a valid key")
    }
    return key
}

// The public key built into the app: the one `return "<base64>"` line inside `let updatePublicKey: String = {` ...
// `}()` of main.swift, read by plain line matching (nothing is evaluated). Anything else than exactly one such line
// holding 32 base64 bytes is an error.
func appPublicKey() -> String {
    let path = option("--main") ?? URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("main.swift").path
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { fail("cannot read \(path)") }
    let lines = text.components(separatedBy: "\n")
    guard let start = lines.firstIndex(of: "let updatePublicKey: String = {"),
          let end = lines[start...].firstIndex(of: "}()") else { fail("no updatePublicKey constant in \(path)") }
    var found: [String] = []
    for line in lines[start..<end] {
        let t = line.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("return \""), t.hasSuffix("\""), t.count > 8 { found.append(String(t.dropFirst(8).dropLast())) }
    }
    guard found.count == 1, let raw = Data(base64Encoded: found[0]), raw.count == 32 else {
        fail("updatePublicKey in \(path) is not exactly one return line with a 32-byte base64 key")
    }
    return found[0]
}

func checkKey() {
    guard loadKey().publicKey.rawRepresentation.base64EncodedString() == appPublicKey() else {
        fail("key mismatch: the public half of \(keyName) in .env is not updatePublicKey in native/main.swift")
    }
    print("ok: the stored key matches updatePublicKey in main.swift")
}

func validVersion(_ v: String) -> Bool {
    let parts = v.split(separator: ".", omittingEmptySubsequences: false)
    return (2...3).contains(parts.count) && v.utf8.count <= 20
        && parts.allSatisfy { !$0.isEmpty && $0.utf8.allSatisfy { $0 >= 48 && $0 <= 57 } }
}

func hex(_ digest: SHA256.Digest) -> String { digest.map { String(format: "%02x", $0) }.joined() }

func zipFacts(_ path: String) -> (sha256: String, size: Int) {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)) else { fail("cannot read \(path)") }
    return (hex(SHA256.hash(data: data)), data.count)
}

func signedText(_ version: String, _ zip: String, _ sha256: String, _ size: Int) -> Data {
    Data("burnboard-release-v1\n\(version)\n\(zip)\n\(sha256)\n\(size)\n".utf8)
}

func expectedURL(_ version: String) -> String {
    #if UPDATE_TEST
    // Test builds only: a fixture server instead of GitHub (the app's test build reads the same variable).
    if let prefix = ProcessInfo.processInfo.environment["BURNBOARD_TEST_ZIP_PREFIX"] {
        return prefix + version + "/Burnboard-" + version + ".zip"
    }
    #endif
    return zipPrefix + version + "/Burnboard-" + version + ".zip"
}

func keygen() {
    let path = envPath()
    if keyLine(path) != nil { fail("a key already exists in \(path); not overwritten") }
    let key = Curve25519.Signing.PrivateKey()
    var text = keyName + "=" + key.rawRepresentation.base64EncodedString() + "\n"
    if let old = FileManager.default.contents(atPath: path), let last = old.last, last != 10 { text = "\n" + text }
    let fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { fail("cannot open \(path) for writing") }
    let bytes = Array(text.utf8)
    let ok = fchmod(fd, 0o600) == 0 && write(fd, bytes, bytes.count) == bytes.count
    close(fd)
    guard ok else { fail("could not write the key to \(path)") }
    print(key.publicKey.rawRepresentation.base64EncodedString())
}

func sign() {
    guard let zip = option("--zip"), let version = option("--version") else { fail("usage: sign --zip PATH --version V [--out PATH]") }
    guard validVersion(version) else { fail("bad version: \(version)") }
    let (sha, size) = zipFacts(zip)
    guard (minSize...maxSize).contains(size) else { fail("the zip is \(size) bytes; the app accepts \(minSize) to \(maxSize)") }
    var url = expectedURL(version)
    #if UPDATE_TEST
    if let u = option("--url") { url = u }  // test builds only: a release whose URL differs
    #endif
    let sig = try! loadKey().signature(for: signedText(version, url, sha, size)).base64EncodedString()
    let json = "{\n  \"version\": \"\(version)\",\n  \"zip\": \"\(url)\",\n  \"sha256\": \"\(sha)\",\n  \"size\": \(size),\n  \"sig\": \"\(sig)\"\n}\n"
    let out = option("--out") ?? "dist/release.json"
    do { try json.write(toFile: out, atomically: true, encoding: .utf8) } catch { fail("cannot write \(out)") }
    print("signed: \(out)")
}

func verify() {
    guard let jsonPath = option("--json"), let zip = option("--zip") else { fail("usage: verify --json PATH --zip PATH [--pubkey B64]") }
    let publicKey: Curve25519.Signing.PublicKey
    if let b64 = option("--pubkey") {
        guard let raw = Data(base64Encoded: b64), let k = try? Curve25519.Signing.PublicKey(rawRepresentation: raw) else { fail("--pubkey is not a valid public key") }
        publicKey = k
    } else {
        publicKey = loadKey().publicKey
    }
    guard let data = FileManager.default.contents(atPath: jsonPath),
          let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let version = o["version"] as? String, let url = o["zip"] as? String, let sha = o["sha256"] as? String,
          let size = (o["size"] as? NSNumber)?.intValue, let sigText = o["sig"] as? String,
          let sig = Data(base64Encoded: sigText) else { fail("release.json has the wrong shape") }
    guard validVersion(version), url == expectedURL(version) else { fail("version or zip URL not as expected") }
    guard publicKey.isValidSignature(sig, for: signedText(version, url, sha, size)) else { fail("signature does not match") }
    let facts = zipFacts(zip)
    guard facts.sha256 == sha.lowercased(), facts.size == size else { fail("the zip does not match sha256 and size in release.json") }
    print("ok: release.json \(version) verifies against \(option("--pubkey") == nil ? "the stored key" : "the given key") and \(zip)")
}

switch CommandLine.arguments.dropFirst().first {
case "keygen": keygen()
case "pubkey": print(loadKey().publicKey.rawRepresentation.base64EncodedString())
case "sign": sign()
case "verify": verify()
case "app-pubkey": print(appPublicKey())
case "check-key": checkKey()
default: fail("usage: release-tool keygen | pubkey | sign --zip PATH --version V [--out PATH] | verify --json PATH --zip PATH [--pubkey B64] | app-pubkey | check-key")
}
