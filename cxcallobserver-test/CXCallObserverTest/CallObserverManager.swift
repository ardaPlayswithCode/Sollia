import Foundation
import CallKit
import ObjectiveC
import Combine
import os.log

// MARK: - What Apple documents about CXCall
//
// Per Apple's public CallKit header (<CallKit/CXCall.h>) as of iOS 17 SDK,
// CXCall exposes exactly five things and nothing else:
//
//     @property (nonatomic, readonly, copy)   NSUUID *UUID;
//     @property (nonatomic, readonly, assign, getter=isOutgoing) BOOL outgoing;
//     @property (nonatomic, readonly, assign, getter=isOnHold)   BOOL onHold;
//     @property (nonatomic, readonly, assign) BOOL hasConnected;
//     @property (nonatomic, readonly, assign) BOOL hasEnded;
//
// There is NO handle, NO remoteHandle, NO phoneNumber, NO contact, NO callee.
// This is not an oversight — see the block comment at the end of this file.
//
// This class exists to *prove* that at runtime, not just take Apple's word:
// on every delegate callback we probe CXCall using Swift's Mirror AND the
// Objective-C runtime (class_copyMethodList / class_copyPropertyList /
// class_copyIvarList), walking the whole class chain up to (but not through)
// NSObject. If any hidden field named handle/number/remote/contact existed,
// this dump would surface it.

final class CallObserverManager: NSObject, ObservableObject, CXCallObserverDelegate {

    private let callObserver = CXCallObserver()
    private let logger = Logger(subsystem: "com.example.CXCallObserverTest", category: "CallObserver")

    @Published var logText: String = ""

    override init() {
        super.init()
        // Delivering on .main lets us safely publish into logText without hopping queues.
        callObserver.setDelegate(self, queue: .main)
        appendBanner("CXCallObserver initialized. Waiting for CXCall events…")
        dumpCXCallMetatype()
    }

    // MARK: - CXCallObserverDelegate

    func callObserver(_ callObserver: CXCallObserver, callChanged call: CXCall) {
        appendBanner("=== CXCALL PROPERTY DUMP (delegate fired) ===")

        // (1) The five documented @property values. These are the ONLY
        // fields the public header exposes. Everything past this point is
        // instrumentation designed to prove there is nothing else.
        let documented = """
        [documented]
          uuid          = \(call.uuid.uuidString)
          isOutgoing    = \(call.isOutgoing)
          isOnHold      = \(call.isOnHold)
          hasConnected  = \(call.hasConnected)
          hasEnded      = \(call.hasEnded)
        """
        append(documented)

        // (2) Swift Mirror. For an @objc class backed by an Objective-C
        // implementation, Mirror.children is usually empty because Obj-C
        // @property storage isn't visible to Swift reflection — but we run
        // it anyway to be exhaustive.
        append("[Swift Mirror(reflecting: call)]")
        let mirror = Mirror(reflecting: call)
        if mirror.children.isEmpty {
            append("  (no children — expected, CXCall's storage is Obj-C)")
        } else {
            for child in mirror.children {
                append("  \(child.label ?? "<nil>") = \(child.value)")
            }
        }

        // (3) Full Obj-C class-chain probe: for every class from
        // type(of: call) up to (but not through) NSObject, dump every
        // @property, method, and ivar. This is the sweep that would
        // catch a hidden "handle" if one existed anywhere in the chain.
        append("[Obj-C class chain for the object]")
        dumpClassChain(startingFrom: type(of: call), indent: "  ")

        // (4) Extra sweep: enumerate every call currently tracked by the
        // observer. Useful if the delegate fires for one call while others
        // are also active (e.g. call waiting, three-way).
        append("[callObserver.calls snapshot (\(self.callObserver.calls.count) tracked)]")
        for (i, c) in self.callObserver.calls.enumerated() {
            append("  [\(i)] uuid=\(c.uuid) outgoing=\(c.isOutgoing) onHold=\(c.isOnHold) connected=\(c.hasConnected) ended=\(c.hasEnded)")
        }

        appendBanner("=== END CXCALL PROPERTY DUMP ===")
    }

    // MARK: - Class-level dump (fires once on launch, and on demand from the UI)

    func dumpCXCallMetatype() {
        appendBanner("=== CXCALL CLASS-LEVEL DUMP (no live call needed) ===")
        // Probe the *declared* class CXCall, in case the runtime object at
        // callback time is a private subclass. We dump both here.
        dumpClassChain(startingFrom: CXCall.self, indent: "  ")
        appendBanner("=== END CXCALL CLASS-LEVEL DUMP ===")
    }

    // MARK: - Utilities

    func clearLog() {
        logText = ""
    }

    /// Walk `cls` and its superclasses up to (but not through) NSObject,
    /// dumping every @property, method, and ivar declared on each level.
    /// This is the actual proof-of-non-existence sweep.
    private func dumpClassChain(startingFrom cls: AnyClass, indent: String) {
        var current: AnyClass? = cls
        while let c = current {
            let name = NSStringFromClass(c)
            append("\(indent)--- class \(name) ---")

            var propCount: UInt32 = 0
            if let props = class_copyPropertyList(c, &propCount) {
                if propCount == 0 { append("\(indent)  (no @property)") }
                for i in 0..<Int(propCount) {
                    let n = String(cString: property_getName(props[i]))
                    let a = property_getAttributes(props[i]).map { String(cString: $0) } ?? ""
                    append("\(indent)  @property \(n) attrs=\(a)")
                }
                free(props)
            }

            var methodCount: UInt32 = 0
            if let methods = class_copyMethodList(c, &methodCount) {
                if methodCount == 0 { append("\(indent)  (no methods)") }
                for i in 0..<Int(methodCount) {
                    let sel = method_getName(methods[i])
                    let n = NSStringFromSelector(sel)
                    append("\(indent)  -[\(name) \(n)]")
                }
                free(methods)
            }

            var ivarCount: UInt32 = 0
            if let ivars = class_copyIvarList(c, &ivarCount) {
                if ivarCount == 0 { append("\(indent)  (no ivars)") }
                for i in 0..<Int(ivarCount) {
                    if let iv = ivars[i] {
                        let n = ivar_getName(iv).map { String(cString: $0) } ?? "?"
                        let t = ivar_getTypeEncoding(iv).map { String(cString: $0) } ?? "?"
                        append("\(indent)  ivar \(n) : \(t)")
                    }
                }
                free(ivars)
            }

            let sc = class_getSuperclass(c)
            if sc == nil { break }
            // Stop before descending into NSObject — its ~40 default methods
            // are noise for this test.
            if NSStringFromClass(sc!) == "NSObject" { break }
            current = sc
        }
    }

    /// Prints a highlighted banner line to both the UI log and the console.
    private func appendBanner(_ line: String) {
        append(line)
    }

    /// Appends one log line. Goes to (a) the on-screen text view, (b) stdout
    /// (visible in the Xcode console), and (c) the unified log (visible in
    /// Console.app / `log stream` — survives even after the app backgrounds).
    private func append(_ line: String) {
        let stamp = Self.timestampFormatter.string(from: Date())
        let entry = "[\(stamp)] \(line)"
        DispatchQueue.main.async {
            self.logText.append(entry + "\n")
        }
        print(entry)
        logger.info("\(entry, privacy: .public)")
    }

    private static let timestampFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()
}

// MARK: - Why the phone number is not, and cannot be, exposed here
//
// CallKit intentionally splits into two roles:
//
//   * CXProvider — implemented by VoIP apps (WhatsApp, Signal, Skype). Those
//     apps place their own IP calls, so they already know the destination
//     handle; they hand it to CallKit only so the system UI can render the
//     call. Third-party observers do NOT see that handle for VoIP calls
//     they didn't originate.
//
//   * CXCallObserver — the passive, read-only role this app uses. It receives
//     call-state transitions so apps can react (pause music, pause game).
//     It intentionally does NOT expose identifying details of calls the
//     observer did not originate.
//
// For a normal cellular voice call, the destination number is known only to
// the modem baseband and the private telephony daemons (CommCenter,
// callservicesd, telephonyutilities). Those daemons never surface the
// dialed digits to third-party, sandboxed apps.
//
// Things people sometimes hope will help but do not:
//   - CoreTelephony's CTCall.dialedID / connectedID: deprecated in iOS 10;
//     was gated by an entitlement Apple did not grant to third parties, and
//     returned nil for outgoing calls even before deprecation.
//   - CXHandle: only meaningful when *your* app populates it via
//     CXStartCallAction. Never populated for cellular calls or for calls
//     initiated by other apps.
//   - CXCallDirectoryExtension: exists to LABEL/BLOCK incoming calls, not
//     to reveal outgoing numbers. Runs in a sandboxed extension process
//     with no callback into the host app when a call happens.
//   - PushKit / VoIP push: reveals what the pusher sends; the SYSTEM
//     telephony stack does not push you anything about cellular calls.
//   - MDM / Managed Configuration: can restrict calling, cannot leak
//     dialed digits to a user-space app.
//   - Accessibility APIs: sandboxed, no telephony surface.
//
// The only ways to obtain the destination number of an outgoing cellular
// call from code on iOS are:
//
//   1. A private / SPI header (Apple-internal). Using one triggers automated
//      App Store review rejection and, in practice, an entitlements check
//      at runtime that a normal signing profile cannot satisfy.
//   2. A jailbroken device with tweaks that hook CommCenter.
//   3. A carrier-privileged app signed by the carrier's SIM applet (very
//      narrow: still doesn't get raw dialed digits in practice).
//
// If someone tells you a public API can do this, this project is the
// falsification test.
