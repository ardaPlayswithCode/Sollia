# CXCallObserver Test

A minimal iOS app whose only purpose is to answer two questions on a real iPhone:

1. **Can a third-party app detect that a phone call is happening?**
2. **Can it identify which phone number is being called?**

**Short answer, before you build anything:**
- Yes to #1. `CXCallObserver` is the correct, documented public API for that. This project uses it.
- **No** to #2, and this project is designed to *prove* that at runtime — not just cite Apple's docs. There is no documented public iOS API that exposes the destination number of an outgoing cellular call to a third-party app.

The runtime dump this app produces is the falsification test: if a "handle" or "phoneNumber" property existed anywhere on `CXCall` or its private superclasses, `class_copyPropertyList` / `class_copyIvarList` would surface it. It doesn't.

---

## What the app does

On launch, it:

1. Instantiates a `CXCallObserver` with `self` as delegate on the main queue.
2. Immediately dumps the `CXCall` class metatype — every `@property`, method, and ivar declared on `CXCall` and its Obj-C superclasses up to `NSObject`. (You get this even without making a call.)

On every call state change, its delegate:

3. Prints the 5 documented public fields: `uuid`, `isOutgoing`, `isOnHold`, `hasConnected`, `hasEnded`.
4. Walks `Mirror(reflecting: call).children` in case any Swift-visible storage exists.
5. Walks the Objective-C class chain of the *concrete runtime class* of the passed-in object (which may be a private subclass) and dumps every `@property`, method, and ivar at each level.
6. Snapshots `callObserver.calls` in case multiple calls are active.

Every event is written to three places:
- **On-screen scrollable log** in the app (so you can see it if the console is closed).
- **`stdout`** — this is what shows up in Xcode's console pane.
- **The unified logging system** (`Logger(subsystem: "com.example.CXCallObserverTest")`) — shows up in **Console.app** and `log stream` and continues capturing even after the app is backgrounded.

Every dump block is bracketed by loud markers so you can grep the console:

```
=== CXCALL PROPERTY DUMP (delegate fired) ===
...
=== END CXCALL PROPERTY DUMP ===
```

---

## What Apple's public documentation says about `CXCall`

Per Apple's `CallKit/CXCall.h` header (iOS 17 SDK), the entire public surface of `CXCall` is:

```objc
@property (nonatomic, readonly, copy)   NSUUID *UUID;
@property (nonatomic, readonly, assign, getter=isOutgoing) BOOL outgoing;
@property (nonatomic, readonly, assign, getter=isOnHold)   BOOL onHold;
@property (nonatomic, readonly, assign) BOOL hasConnected;
@property (nonatomic, readonly, assign) BOOL hasEnded;
```

That's it. No `handle`, `remoteHandle`, `phoneNumber`, `contact`, `callee`, or `destination`. Nothing that identifies the party on the other end.

## Why the number is not, and cannot be, accessed via CallKit

CallKit is structured into two roles:

- **`CXProvider`** — implemented by VoIP apps (WhatsApp, Signal, Skype). These place their own IP calls, so they already know the destination handle; they hand it to the system only so the call renders in the native UI. Third-party observers do not see that handle for calls they didn't originate.
- **`CXCallObserver`** — the passive, read-only role this test app uses. Designed for "pause the game / mute the video when a call starts" use cases. It intentionally does not expose identifying details of calls the observer didn't originate.

For a plain cellular call, the destination number is known only to the modem baseband and Apple's private telephony daemons (`CommCenter`, `callservicesd`, `telephonyutilities`). Those daemons never surface dialed digits to third-party sandboxed apps.

**Adjacent APIs people sometimes reach for and why they don't work either:**

| API | Why it doesn't give you the outgoing number |
|-----|---------------------------------------------|
| `CoreTelephony.CTCall` (`dialedID`, `connectedID`) | Deprecated in iOS 10. Required an entitlement Apple never granted to third parties. Returned `nil` for outgoing calls even before deprecation. |
| `CXHandle` | Only populated when *your* app originates the call via `CXStartCallAction`. Not populated for cellular calls or calls started by other apps. |
| `CXCallDirectoryExtension` | Purpose is to *label / block incoming* calls (Truecaller-style). Runs in a sandboxed extension with no callback into the host app when a call happens. Cannot see outbound numbers. |
| `PushKit` / VoIP push | Reveals only what the pusher sent. The system telephony stack does not push you cellular-call metadata. |
| `MDM` / Managed Configuration | Can restrict calling; enforcement is at the modem level. No payload leaks dialed digits to a user-space app. |
| Accessibility, Screen Time APIs | Sandboxed, no telephony surface. |

**Could a special entitlement unlock it?** No public entitlement grants "read outgoing call destinations." The CallKit VoIP entitlement is required only to *start* calls as a `CXProvider`. It doesn't expand what `CXCallObserver` sees.

**Would getting it require a private API?** Yes. The only ways to obtain the destination number of an outgoing cellular call from code on iOS are:
1. A private/SPI header (Apple-internal). Triggers App Store review rejection; the underlying entitlements check would also fail at runtime under a normal signing profile.
2. A jailbroken device with a tweak that hooks `CommCenter`.
3. A carrier-privileged app signed by the carrier's SIM applet — very narrow, still doesn't get raw dialed digits in practice.

---

## Files

```
cxcallobserver-test/
├── README.md                                    ← this file
├── CXCallObserverTest.xcodeproj/                ← the Xcode project
└── CXCallObserverTest/
    ├── CXCallObserverTestApp.swift              ← @main entry point (SwiftUI)
    ├── ContentView.swift                        ← on-screen scrollable log
    ├── CallObserverManager.swift                ← THE CORE: sets up CXCallObserver
    │                                              and does the runtime dump
    └── Info.plist
```

`CallObserverManager.swift` is the file to read. Its top and bottom comments spell out exactly what CallKit exposes and doesn't, and why.

---

## Setup on a real iPhone — step by step

Assumes a fresh macOS + Xcode install; you don't need a paid Apple Developer account.

### Prerequisite: install Xcode and point the CLI tools at it

If Xcode isn't already installed, install it from the Mac App Store. Then run this once so the terminal knows to use Xcode's tools instead of the standalone Command Line Tools:

```bash
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
```

(You'll be asked for your Mac password.)

### Step 1: Open the project

```bash
open "cxcallobserver-test/CXCallObserverTest.xcodeproj"
```

Xcode will open and show the project. If Xcode asks about downloading platforms, allow it — iOS support may need to be installed the first time.

### Step 2: Set a signing team and a unique Bundle ID

1. In Xcode's left sidebar (Project navigator), click the blue **CXCallObserverTest** project icon at the top.
2. In the middle pane, click the **CXCallObserverTest** target (under **TARGETS**).
3. Open the **Signing & Capabilities** tab.
4. Tick **Automatically manage signing**.
5. Under **Team**, pick your personal team (your Apple ID). If it's not there:
   - Click **Add an Account…**
   - Sign in with your Apple ID
   - Return to Signing & Capabilities and select the newly listed personal team
6. Change **Bundle Identifier** from `com.example.CXCallObserverTest` to something unique to you, e.g. `com.<yourname>.CXCallObserverTest`. Free personal teams reject namespace collisions with other developers.

### Step 3: Entitlements / capabilities — none needed

`CXCallObserver` requires **no entitlement**. Passive observation of call state has no permission gate. Do **not** add "Voice over IP" or Background Modes → voip; those are for VoIP `CXProvider` apps and would only complicate the profile without unlocking anything.

Also **no `Info.plist` privacy strings** are needed for this test — the app doesn't access Contacts, the microphone, or CoreTelephony subscriber info.

### Step 4: Connect your iPhone

1. Plug the iPhone into the Mac with a Lightning/USB-C cable.
2. On the iPhone, tap **Trust** when the "Trust This Computer?" dialog appears; enter your passcode.
3. In Xcode's top toolbar, click the **run-destination selector** (the pill next to the play/stop buttons that shows the current scheme + destination) and pick your iPhone by name from the **Device** section.
   - Do **not** pick any Simulator entry — the iOS Simulator cannot make real cellular calls, so `CXCallObserver` will never fire there.
   - Do **not** pick "Any iOS Device (arm64)" — that's for archiving, not running.

If Xcode says "Preparing <your iPhone> for development" it may take 5–10 minutes the first time as it downloads debug symbols matching your iOS version. Let it finish.

### Step 5: Build & run — expect one "Untrusted Developer" detour

Press **⌘R** (or click the ▶︎ Run button).

The first build will finish, install to the device, and immediately fail to launch with an **"Untrusted Developer"** message on the iPhone. This is normal for a free personal team on a fresh install. Fix it once:

1. On the iPhone: **Settings → General → VPN & Device Management** (older iOS: **Settings → General → Device Management**).
2. Under **DEVELOPER APP** tap your Apple ID → **Trust "<your name>"** → **Trust**.
3. Back in Xcode press **⌘R** again. This time the app launches.

You should now see the app running with the on-screen log view saying `(No events yet — waiting on CXCallObserver…)` plus the initial `=== CXCALL CLASS-LEVEL DUMP ===` block.

> ⚠️ Personal-team apps expire after **7 days**. If the icon goes gray with an error, just plug the phone back in and re-run **⌘R** to reinstall.

### Step 6: Watch the console

In Xcode: **View → Debug Area → Activate Console** (or **⇧⌘Y**). Everything the app writes to `stdout` appears here.

For log capture that survives the app going to background, use **Console.app** on the Mac instead:
1. Launch **Console.app** (Cmd+Space, type "Console").
2. In the sidebar, select your iPhone under **Devices**.
3. Click **Start** at the top.
4. In the search bar type: `subsystem:com.example.CXCallObserverTest` (or use the target-specific `PROCESS` filter with `CXCallObserverTest`).

You can also live-stream from the terminal after installing Xcode:

```bash
xcrun devicectl device console --device "<your-device-name>"
```

### Step 7: Make an outgoing call

Keep the app in the foreground for the most reliable capture. (`CXCallObserver` does receive backgrounded callbacks in some cases, but iOS may suspend a plain background app before all state transitions fire; the on-screen log lets you resume the app after the call and still see what was captured while it stayed alive.)

1. **Slide up** from the bottom of the screen (or double-tap Home on Touch ID iPhones) — do NOT force-quit the test app.
2. Open **Phone** and dial a real number. Any number will do; if you don't want to actually connect, calling your carrier's info line (e.g. **611** on many US carriers) is a safe test.
3. Once the call ends, return to the **CXCallObserverTest** app. The scroll view will show the events that fired while the call was active.
4. In the Xcode console or Console.app, search for `CXCALL PROPERTY DUMP` — every entry the delegate saw will be there.

### What you should observe in the output

- `isOutgoing = true` on the initial dump for an outgoing call.
- `hasConnected` flipping to `true` if/when the callee answers.
- `hasEnded` flipping to `true` on hangup.
- **Nothing anywhere** — not in the documented block, not in the Swift Mirror walk, not in `class_copyPropertyList`, not in `class_copyMethodList`, not in `class_copyIvarList`, not on any private superclass in the chain — that names a phone number, handle, remote party, contact, callee, destination, or dialed digit string.

That absence is the whole point of the exercise.

---

## The plain-language answer

**Can this approach detect that a call is happening?** Yes. `CXCallObserver` is the correct public API and this project demonstrates it working end-to-end.

**Can it identify which number was called?** No. Not through `CXCall`, not through any documented adjacent CallKit or CoreTelephony API, and not with any entitlement Apple grants to third-party developers. The destination number of an outgoing cellular call is intentionally never surfaced to third-party apps on iOS. Extracting it would require a private API, a jailbreak-level tweak, or a carrier-privileged signing arrangement — none of which are options for an ordinary App Store app.
