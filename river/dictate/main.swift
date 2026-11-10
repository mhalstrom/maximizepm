// maxpm-dictate (#5107): speech to text on a Mac with Apple's SpeechAnalyzer and SpeechTranscriber (macOS 26), on the
// device. maxpm serve runs one for each dictation of the page: the page records the microphone and sends the audio to
// serve, and serve writes it here. So this program reads audio only from serve (stdin) and needs no microphone of its
// own.
//
// stdin: 16-bit little-endian mono PCM at 16000 Hz, until end of file (the person stopped).
// stdout: one JSON object on each line:
//   {"installing": "<locale>"}  macOS downloads the speech model of the language (once)
//   {"ready": "<locale>"}       it listens
//   {"partial": "<text>"}       the words of the part not final yet (they can still change)
//   {"final": "<text>"}         a part that is final
//   {"done": true}              all audio is read; the program ends
//   {"error": "<why>"}          and the program ends with exit code 1
// --check: print {"ok": true, "locale": ..., "installed": bool} (or {"ok": false, "error": ...}) and end.
import AVFoundation
import Foundation
import Speech

let rate = 16000.0
var localeId = Locale.current.identifier
var checkOnly = false
var args = CommandLine.arguments.dropFirst().makeIterator()
while let a = args.next() {
    if a == "--locale", let v = args.next() { localeId = v } else if a == "--check" { checkOnly = true }
}

let out = FileHandle.standardOutput
func say(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) {
        out.write((s + "\n").data(using: .utf8)!)
    }
}

func fail(_ why: String) -> Never {
    say(["error": why])
    exit(1)
}

func run() async {
    guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: localeId)) else {
        if checkOnly { say(["ok": false, "error": "no speech model for \(localeId) on this Mac"]); exit(0) }
        fail("macOS has no speech model for the language \(localeId)")
    }
    let name = locale.identifier(.bcp47)
    let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [.volatileResults, .fastResults],
                                        attributeOptions: [])
    let installed = await SpeechTranscriber.installedLocales.contains { $0.identifier(.bcp47) == name }
    if checkOnly {
        // The permission of speech recognition (SFSpeechRecognizer): SpeechAnalyzer on the device works without it.
        let auth = ["notDetermined", "denied", "restricted", "authorized"][SFSpeechRecognizer.authorizationStatus().rawValue]
        say(["ok": true, "locale": name, "installed": installed, "speech_permission": auth])
        exit(0)
    }
    do {
        if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            if !installed { say(["installing": name]) }
            try await request.downloadAndInstall()
        }
    } catch {
        fail("macOS could not install the speech model for \(name): \(error.localizedDescription)")
    }
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
        fail("the speech model of \(name) names no audio format")
    }
    let inFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: rate, channels: 1, interleaved: true)!
    guard let converter = AVAudioConverter(from: inFormat, to: format) else {
        fail("no converter from 16 kHz audio to \(format)")
    }
    let (stream, feed) = AsyncStream<AnalyzerInput>.makeStream()
    let results = Task {
        do {
            for try await r in transcriber.results {
                let text = String(r.text.characters)
                say(r.isFinal ? ["final": text] : ["partial": text])
            }
        } catch {
            fail("speech recognition stopped: \(error.localizedDescription)")
        }
    }
    do {
        try await analyzer.start(inputSequence: stream)
    } catch {
        fail("speech recognition did not start: \(error.localizedDescription)")
    }
    say(["ready": name])
    // Read stdin in pieces of 100 ms and hand each one, converted, to the analyzer.
    let stdin = FileHandle.standardInput
    var carry = Data()
    while true {
        let chunk = stdin.availableData
        if chunk.isEmpty { break }
        carry.append(chunk)
        let usable = carry.count - carry.count % 2
        if usable == 0 { continue }
        let frames = AVAudioFrameCount(usable / 2)
        guard let buf = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: frames) else { continue }
        buf.frameLength = frames
        _ = carry.prefix(usable).withUnsafeBytes { raw in
            memcpy(buf.int16ChannelData![0], raw.baseAddress!, usable)
        }
        carry.removeFirst(usable)
        let cap = AVAudioFrameCount(Double(frames) * format.sampleRate / rate) + 32
        guard let conv = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: cap) else { continue }
        var given = false
        var err: NSError?
        converter.convert(to: conv, error: &err) { _, status in
            if given { status.pointee = .noDataNow; return nil }
            given = true
            status.pointee = .haveData
            return buf
        }
        if let err { fail("audio conversion: \(err.localizedDescription)") }
        if conv.frameLength > 0 { feed.yield(AnalyzerInput(buffer: conv)) }
    }
    feed.finish()
    do {
        try await analyzer.finalizeAndFinishThroughEndOfInput()
    } catch {
        fail("speech recognition did not finish: \(error.localizedDescription)")
    }
    _ = await results.result
    say(["done": true])
    exit(0)
}

Task { await run() }
dispatchMain()
