import UIKit

/// Native launch intro: the MATCHRIX node network assembles itself (matching
/// the app icon's "M" graph), breathes gently, then fades out to reveal the
/// app underneath. Runs as a plain overlay view, no third-party dependency.
final class MatchrixSplashView: UIView {

    private struct NodeSpec {
        let x: CGFloat
        let y: CGFloat
    }

    // Mirrors the node graph baked into the app icon (see mobile/resources/icon.png):
    // logical canvas is a 200x200 box, x in [150,350], y in [160,360].
    private static let nodes: [NodeSpec] = [
        NodeSpec(x: 150, y: 160), NodeSpec(x: 150, y: 260), NodeSpec(x: 150, y: 360),
        NodeSpec(x: 190, y: 210), NodeSpec(x: 190, y: 310), NodeSpec(x: 225, y: 180),
        NodeSpec(x: 225, y: 270), NodeSpec(x: 250, y: 230), NodeSpec(x: 250, y: 320),
        NodeSpec(x: 275, y: 180), NodeSpec(x: 275, y: 270), NodeSpec(x: 310, y: 210),
        NodeSpec(x: 310, y: 310), NodeSpec(x: 350, y: 160), NodeSpec(x: 350, y: 260),
        NodeSpec(x: 350, y: 360), NodeSpec(x: 200, y: 360), NodeSpec(x: 300, y: 360),
    ]

    // 1-indexed node ids, matching the icon's connecting edges.
    private static let connections: [(Int, Int)] = [
        (1, 2), (2, 3), (1, 4), (4, 5), (3, 17), (4, 6), (5, 7),
        (6, 8), (7, 9), (8, 10), (9, 11), (10, 12), (11, 13),
        (12, 14), (13, 15), (14, 15), (15, 16), (13, 18), (17, 9), (9, 18),
    ]

    private static let boxMinX: CGFloat = 150
    private static let boxMinY: CGFloat = 160
    private static let boxSize: CGFloat = 200

    // Timing tuned for a ~3.7s total intro (stagger-in -> settle hold -> fade).
    private static let fps = 60.0
    private static let staggerWindow = 90.0     // frames over which the 18 nodes start, in order
    private static let nodeDuration = 35.0      // each node's own reveal (opacity/position/scale) length
    private static let connectionDelay = 6.0    // frames after both endpoints land before a line draws
    private static let connectionDuration = 20.0
    private static let holdAfterAssembly = 1.0  // seconds the finished logo breathes before fading
    private static let fadeDuration = 0.55

    private let glowLayer = CAGradientLayer()
    private let networkContainer = CALayer()
    private let wordmarkLabel = UILabel()
    private var completion: (() -> Void)?

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = UIColor(red: 3/255.0, green: 7/255.0, blue: 18/255.0, alpha: 1)

        glowLayer.type = .radial
        glowLayer.colors = [
            UIColor(red: 0.0, green: 0.6, blue: 0.75, alpha: 0.28).cgColor,
            UIColor(red: 0.0, green: 0.6, blue: 0.75, alpha: 0.0).cgColor,
        ]
        glowLayer.startPoint = CGPoint(x: 0.5, y: 0.5)
        glowLayer.endPoint = CGPoint(x: 1.0, y: 1.0)
        glowLayer.opacity = 0
        layer.addSublayer(glowLayer)

        layer.addSublayer(networkContainer)

        wordmarkLabel.text = "MATCHRIX"
        wordmarkLabel.font = .systemFont(ofSize: 26, weight: .heavy)
        wordmarkLabel.textColor = .white
        wordmarkLabel.textAlignment = .center
        wordmarkLabel.alpha = 0
        if let kerned = wordmarkLabel.attributedText.map({ NSMutableAttributedString(attributedString: $0) }) {
            kerned.addAttribute(.kern, value: 2.0, range: NSRange(location: 0, length: kerned.length))
            wordmarkLabel.attributedText = kerned
        }
        addSubview(wordmarkLabel)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layoutSubviews() {
        super.layoutSubviews()
        let side = min(bounds.width, bounds.height) * 0.55
        let origin = CGPoint(x: bounds.midX - side / 2, y: bounds.midY - side / 2)
        networkContainer.frame = CGRect(origin: origin, size: CGSize(width: side, height: side))

        let glowSide = side * 2.2
        glowLayer.frame = CGRect(
            x: bounds.midX - glowSide / 2,
            y: bounds.midY - glowSide / 2,
            width: glowSide,
            height: glowSide
        )

        wordmarkLabel.frame = CGRect(
            x: bounds.midX - side,
            y: origin.y + side + 18,
            width: side * 2,
            height: 34
        )
    }

    private func point(for spec: NodeSpec, in size: CGSize) -> CGPoint {
        let nx = (spec.x - Self.boxMinX) / Self.boxSize
        let ny = (spec.y - Self.boxMinY) / Self.boxSize
        return CGPoint(x: nx * size.width, y: ny * size.height)
    }

    /// Blue (left) -> green (right) gradient matching the app icon.
    private func color(forX x: CGFloat) -> UIColor {
        let t = max(0, min(1, (x - Self.boxMinX) / Self.boxSize))
        let start = (r: 0.22, g: 0.51, b: 0.95)   // #3b82f6-ish
        let end   = (r: 0.02, g: 0.59, b: 0.41)   // #059669-ish
        return UIColor(
            red: CGFloat(start.r + (end.r - start.r) * t),
            green: CGFloat(start.g + (end.g - start.g) * t),
            blue: CGFloat(start.b + (end.b - start.b) * t),
            alpha: 1
        )
    }

    private func startFrame(forIdx idx: Int) -> Double {
        Double(Int((Double(idx) / 18.0) * Self.staggerWindow))
    }

    private func endFrame(forIdx idx: Int) -> Double {
        startFrame(forIdx: idx) + Self.nodeDuration
    }

    /// Plays the assembly animation once, then fades this view out and calls completion.
    func playIntro(completion: @escaping () -> Void) {
        self.completion = completion
        let size = networkContainer.bounds.size
        guard size.width > 0, size.height > 0 else {
            // Layout hasn't happened yet; try again next runloop turn.
            DispatchQueue.main.async { [weak self] in self?.playIntro(completion: completion) }
            return
        }

        let fps = Self.fps
        var maxLineEndFrame: Double = 0

        // Lines first so nodes render in front of them.
        for (a, b) in Self.connections {
            let specA = Self.nodes[a - 1]
            let specB = Self.nodes[b - 1]
            let start = max(endFrame(forIdx: a - 1), endFrame(forIdx: b - 1)) + Self.connectionDelay
            let end = start + Self.connectionDuration
            maxLineEndFrame = max(maxLineEndFrame, end)
            addLine(from: point(for: specA, in: size), to: point(for: specB, in: size),
                    startTime: start / fps, duration: (end - start) / fps)
        }

        for (idx, spec) in Self.nodes.enumerated() {
            let start = startFrame(forIdx: idx) / fps
            let dur = (endFrame(forIdx: idx) - startFrame(forIdx: idx)) / fps
            addNode(at: point(for: spec, in: size), color: color(forX: spec.x),
                    startTime: start, duration: dur)
        }

        let assemblyDuration = maxLineEndFrame / fps
        fadeInGlow(after: assemblyDuration * 0.3)
        startBreathing(after: assemblyDuration)
        revealWordmark(after: assemblyDuration)

        let totalDuration = assemblyDuration + Self.holdAfterAssembly
        DispatchQueue.main.asyncAfter(deadline: .now() + totalDuration) { [weak self] in
            self?.fadeOutAndFinish()
        }
    }

    private func fadeInGlow(after delay: Double) {
        let anim = CABasicAnimation(keyPath: "opacity")
        anim.fromValue = 0
        anim.toValue = 1
        anim.duration = 1.2
        anim.beginTime = CACurrentMediaTime() + delay
        anim.fillMode = .both
        anim.isRemovedOnCompletion = false
        glowLayer.add(anim, forKey: "glowIn")
    }

    private func revealWordmark(after delay: Double) {
        wordmarkLabel.transform = CGAffineTransform(translationX: 0, y: 8)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self else { return }
            UIView.animate(withDuration: 0.5, delay: 0, options: .curveEaseOut, animations: {
                self.wordmarkLabel.alpha = 1
                self.wordmarkLabel.transform = .identity
            })
        }
    }

    /// Subtle breathing pulse on the assembled logo so the hold beat feels alive, not static.
    private func startBreathing(after delay: Double) {
        let pulse = CABasicAnimation(keyPath: "transform.scale")
        pulse.fromValue = 1.0
        pulse.toValue = 1.035
        pulse.duration = 1.1
        pulse.autoreverses = true
        pulse.repeatCount = .infinity
        pulse.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        pulse.beginTime = CACurrentMediaTime() + delay
        networkContainer.add(pulse, forKey: "breathe")
    }

    private func addNode(at point: CGPoint, color: UIColor, startTime: Double, duration: Double) {
        let radius: CGFloat = 8
        let shape = CAShapeLayer()
        shape.path = UIBezierPath(ovalIn: CGRect(x: -radius, y: -radius, width: radius * 2, height: radius * 2)).cgPath
        shape.fillColor = color.cgColor
        shape.position = point
        shape.opacity = 0
        shape.shadowColor = color.cgColor
        shape.shadowOpacity = 0.7
        shape.shadowRadius = 6
        shape.shadowOffset = .zero
        networkContainer.addSublayer(shape)

        let angle = Double(networkContainer.sublayers?.count ?? 0)
        let scatterStart = CGPoint(
            x: point.x + CGFloat(cos(angle)) * 60,
            y: point.y + CGFloat(sin(angle)) * 60
        )

        let opacity = CABasicAnimation(keyPath: "opacity")
        opacity.fromValue = 0
        opacity.toValue = 1

        let position = CAKeyframeAnimation(keyPath: "position")
        position.values = [scatterStart, point].map { NSValue(cgPoint: $0) }
        position.keyTimes = [0, 1]

        let scale = CAKeyframeAnimation(keyPath: "transform.scale")
        scale.values = [0, 1.3, 1.0]
        scale.keyTimes = [0, 0.7, 1.0]

        let group = CAAnimationGroup()
        group.animations = [opacity, position, scale]
        group.duration = duration
        group.beginTime = CACurrentMediaTime() + startTime
        group.fillMode = .both
        group.isRemovedOnCompletion = false
        group.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        shape.add(group, forKey: "intro")
    }

    private func addLine(from a: CGPoint, to b: CGPoint, startTime: Double, duration: Double) {
        let shape = CAShapeLayer()
        let path = UIBezierPath()
        path.move(to: a)
        path.addLine(to: b)
        shape.path = path.cgPath
        shape.strokeColor = UIColor(red: 0.0, green: 0.85, blue: 0.9, alpha: 0.55).cgColor
        shape.fillColor = UIColor.clear.cgColor
        shape.lineWidth = 2
        shape.lineCap = .round
        shape.strokeEnd = 0
        networkContainer.insertSublayer(shape, at: 0)

        let draw = CABasicAnimation(keyPath: "strokeEnd")
        draw.fromValue = 0
        draw.toValue = 1
        draw.duration = duration
        draw.beginTime = CACurrentMediaTime() + startTime
        draw.fillMode = .both
        draw.isRemovedOnCompletion = false
        draw.timingFunction = CAMediaTimingFunction(name: .easeOut)
        shape.add(draw, forKey: "draw")
    }

    private func fadeOutAndFinish() {
        UIView.animate(withDuration: Self.fadeDuration, animations: {
            self.alpha = 0
        }, completion: { _ in
            self.removeFromSuperview()
            self.completion?()
        })
    }
}
