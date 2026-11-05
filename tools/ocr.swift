// OCR helper: Apple Vision text recognition. usage: ocr <image> [min_confidence]
// Prints JSON: [{text, confidence, box:[x,y,w,h], words:[{text, box}]}] with 0..1 top-left coordinates.
import Foundation
import Vision
import ImageIO

func box(_ r: CGRect) -> [Double] {
    [Double(r.minX), Double(1 - r.maxY), Double(r.width), Double(r.height)]
}

func recognize(_ path: String, _ minConfidence: Float) -> Data? {
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { return nil }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.automaticallyDetectsLanguage = true
    guard (try? VNImageRequestHandler(cgImage: image, options: [:]).perform([request])) != nil else { return nil }
    var out: [[String: Any]] = []
    for observation in request.results ?? [] {
        guard let top = observation.topCandidates(1).first, top.confidence >= minConfidence else { continue }
        var words: [[String: Any]] = []
        let text = top.string
        text.enumerateSubstrings(in: text.startIndex..<text.endIndex, options: .byWords) { word, range, _, _ in
            if let word = word, let rect = try? top.boundingBox(for: range) {
                words.append(["text": word, "box": box(rect.boundingBox)])
            }
        }
        out.append(["text": text, "confidence": Double(top.confidence), "box": box(observation.boundingBox), "words": words])
    }
    return try? JSONSerialization.data(withJSONObject: out)
}

let args = CommandLine.arguments
if args.count >= 2 && args[1] == "--serve" {
    // Persistent mode: each stdin line is "<min_confidence>\t<image path>"; each answer is one JSON line.
    while let line = readLine() {
        let parts = line.split(separator: "\t", maxSplits: 1).map(String.init)
        guard parts.count == 2 else { print("{\"error\":\"bad request\"}"); fflush(stdout); continue }
        if let data = recognize(parts[1], Float(parts[0]) ?? 0.3), let text = String(data: data, encoding: .utf8) {
            print(text)
        } else {
            print("{\"error\":\"cannot read image\"}")
        }
        fflush(stdout)
    }
} else if args.count >= 2 {
    guard let data = recognize(args[1], args.count >= 3 ? Float(args[2]) ?? 0.3 : 0.3) else {
        FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!); exit(2)
    }
    FileHandle.standardOutput.write(data)
} else {
    FileHandle.standardError.write("usage: ocr <image> [min_confidence] | ocr --serve\n".data(using: .utf8)!); exit(2)
}
