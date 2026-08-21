import SwiftUI

struct ContentView: View {
    @StateObject private var manager = CallObserverManager()

    var body: some View {
        NavigationView {
            VStack(spacing: 0) {
                Text("Make an outgoing call while this app is running. Every CXCallObserver event is written below and to the Xcode console. Search the console for: CXCALL PROPERTY DUMP")
                    .font(.footnote)
                    .padding()
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color(.secondarySystemBackground))

                ScrollViewReader { proxy in
                    ScrollView {
                        Text(manager.logText.isEmpty ? "(No events yet — waiting on CXCallObserver…)" : manager.logText)
                            .font(.system(.caption2, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding()
                            .textSelection(.enabled)
                            .id("bottom")
                    }
                    .onChange(of: manager.logText) { _ in
                        withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                    }
                }

                HStack {
                    Button("Clear log") { manager.clearLog() }
                        .buttonStyle(.bordered)
                    Spacer()
                    Button("Re-dump CXCall class") { manager.dumpCXCallMetatype() }
                        .buttonStyle(.borderedProminent)
                }
                .padding()
            }
            .navigationTitle("CXCallObserver")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
