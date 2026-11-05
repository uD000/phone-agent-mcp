// Read-only Mac Contacts export. usage: contacts [OUT.json]  -> JSON [{name, phones:[{label, number}]}]
// With OUT.json it writes the list there and a status line to OUT.json.status (needed when started as an .app).
// Needs the user's Contacts permission (macOS asks once). Never writes anything.
import Contacts
import Foundation

let store = CNContactStore()
let status = CNContactStore.authorizationStatus(for: .contacts)
let names = [0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized", 4: "limited"]
FileHandle.standardError.write("status before: \(names[status.rawValue] ?? "?")\n".data(using: .utf8)!)
let gate = DispatchSemaphore(value: 0)
var granted = false
store.requestAccess(for: .contacts) { ok, _ in granted = ok; gate.signal() }
gate.wait()
let outPath: String? = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : nil
func status(_ text: String) { if let p = outPath { try? text.write(toFile: p + ".status", atomically: true, encoding: .utf8) } }
guard granted else {
    status("denied")
    let after = CNContactStore.authorizationStatus(for: .contacts)
    FileHandle.standardError.write("Contacts access not granted (status now: \(names[after.rawValue] ?? "?"))\n".data(using: .utf8)!); exit(3)
}
let keys: [CNKeyDescriptor] = [CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
                               CNContactOrganizationNameKey as CNKeyDescriptor, CNContactNicknameKey as CNKeyDescriptor,
                               CNContactPhoneNumbersKey as CNKeyDescriptor]
var out: [[String: Any]] = []
var total = 0
try store.enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) { c, _ in
    var name = CNContactFormatter.string(from: c, style: .fullName) ?? ""
    if name.isEmpty { name = c.nickname.isEmpty ? c.organizationName : c.nickname }
    total += 1
    if name.isEmpty || c.phoneNumbers.isEmpty { return }
    let phones: [[String: String]] = c.phoneNumbers.map {
        ["label": CNLabeledValue<NSString>.localizedString(forLabel: $0.label ?? ""), "number": $0.value.stringValue]
    }
    out.append(["name": name, "phones": phones])
}
let payload = try JSONSerialization.data(withJSONObject: out)
if let p = outPath {
    FileManager.default.createFile(atPath: p, contents: payload, attributes: [.posixPermissions: 0o600])
    status("ok \(out.count) \(total)")
} else {
    FileHandle.standardOutput.write(payload)
}
