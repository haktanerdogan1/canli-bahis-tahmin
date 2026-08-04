import UIKit
import WebKit
import Capacitor

class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }

        // App yalnizca uzaktaki (Railway) siteyi gosteren bir kabuk - kalici bir
        // WKWebView onbellegi burada hicbir fayda saglamiyor, sadece her sunucu
        // guncellemesinden sonra eski sayfanin gorunmesine yol aciyordu. Her
        // acilista temizleyip her zaman en guncel siteyi cekmesini garantiliyoruz.
        WKWebsiteDataStore.default().removeData(
            ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(),
            modifiedSince: .distantPast
        ) { [weak self] in
            self?.setUpWindow(in: windowScene)
        }

        SceneDelegateProxy.shared.scene(scene, willConnectTo: session, options: connectionOptions)
    }

    private func setUpWindow(in windowScene: UIWindowScene) {
        window = UIWindow(windowScene: windowScene)
        window?.rootViewController = CAPBridgeViewController()
        window?.makeKeyAndVisible()

        if let window = window {
            let splash = MatchrixSplashView(frame: window.bounds)
            splash.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            window.addSubview(splash)
            splash.layoutIfNeeded()
            splash.playIntro { }
        }
    }

    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        SceneDelegateProxy.shared.scene(scene, openURLContexts: URLContexts)
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        SceneDelegateProxy.shared.scene(scene, continue: userActivity)
    }
}
