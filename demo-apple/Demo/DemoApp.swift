import SwiftUI
import MapKit

// Demo: Apple Maps (MapKit) con relieve real, en primera persona por la CV-70, con la ruta y el icono de ClusterRide.
// Tres tramos seguidos: Estándar de día, Híbrido (foto) y Estándar de noche.
@main struct DemoApp: App {
    var body: some Scene { WindowGroup { ContentView() } }
}

struct Arrow: View {
    var body: some View {
        Path { p in
            p.move(to: CGPoint(x: 22, y: 0)); p.addLine(to: CGPoint(x: 42, y: 40))
            p.addLine(to: CGPoint(x: 22, y: 30)); p.addLine(to: CGPoint(x: 2, y: 40)); p.closeSubpath()
        }
        .fill(.white)
        .overlay(Path { p in
            p.move(to: CGPoint(x: 22, y: 0)); p.addLine(to: CGPoint(x: 42, y: 40))
            p.addLine(to: CGPoint(x: 22, y: 30)); p.addLine(to: CGPoint(x: 2, y: 40)); p.closeSubpath()
        }.stroke(Color(red: 1, green: 0.6, blue: 0.12), style: StrokeStyle(lineWidth: 5, lineJoin: .round)))
        .frame(width: 44, height: 42)
        .shadow(color: .black.opacity(0.4), radius: 6, y: 5)
    }
}

final class Sim: ObservableObject {
    let pts = Route.points
    var cum: [Double] = [0]
    @Published var pos = Route.points[0]
    @Published var heading = 0.0
    @Published var phase = 0
    init() {
        for i in 1..<pts.count { cum.append(cum[i - 1] + dist(pts[i - 1], pts[i])) }
    }
    func dist(_ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D) -> Double {
        CLLocation(latitude: a.latitude, longitude: a.longitude).distance(from: CLLocation(latitude: b.latitude, longitude: b.longitude))
    }
    func at(_ s: Double) -> CLLocationCoordinate2D {
        let s = max(0, min(cum.last!, s))
        for i in 1..<cum.count where cum[i] >= s {
            let t = (s - cum[i - 1]) / max(0.001, cum[i] - cum[i - 1]); let a = pts[i - 1], b = pts[i]
            return CLLocationCoordinate2D(latitude: a.latitude + (b.latitude - a.latitude) * t, longitude: a.longitude + (b.longitude - a.longitude) * t)
        }
        return pts.last!
    }
    func bearing(_ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D) -> Double {
        let la1 = a.latitude * .pi / 180, la2 = b.latitude * .pi / 180, dl = (b.longitude - a.longitude) * .pi / 180
        return atan2(sin(dl) * cos(la2), cos(la1) * sin(la2) - sin(la1) * cos(la2) * cos(dl)) * 180 / .pi
    }
}

struct ContentView: View {
    @StateObject var sim = Sim()
    @State var cam: MapCameraPosition = .camera(MapCamera(centerCoordinate: Route.points[0], distance: 500, heading: 0, pitch: 70))
    @State var s = 0.0
    @State var head: Double? = nil
    let speed = 55.0       // m por segundo de vídeo (cámara algo rápida para ver más tramo)
    let tick = 0.1

    var style: MapStyle {
        switch sim.phase {
        case 2: return .hybrid(elevation: .realistic, pointsOfInterest: .excludingAll)
        default: return .standard(elevation: .realistic, emphasis: .automatic, pointsOfInterest: .excludingAll, showsTraffic: false)
        }
    }

    var body: some View {
        Map(position: $cam, interactionModes: []) {
            MapPolyline(coordinates: sim.pts).stroke(Color(red: 1, green: 0.6, blue: 0.12).opacity(0.55), lineWidth: 16)
            MapPolyline(coordinates: sim.pts).stroke(Color(red: 1, green: 0.6, blue: 0.12), lineWidth: 9)
            MapPolyline(coordinates: sim.pts).stroke(.white, lineWidth: 2.5)
            Annotation("", coordinate: sim.pos, anchor: .center) { Arrow().rotation3DEffect(.degrees(55), axis: (x: 1, y: 0, z: 0)) }
        }
        .mapStyle(style)
        .mapControlVisibility(.hidden)
        .preferredColorScheme(sim.phase == 3 ? .dark : .light)
        .ignoresSafeArea()
        .overlay(alignment: .top) {
            Text(["Apple Maps · Estándar (día)", "Apple Maps · Vista aérea de la sierra", "Apple Maps · Híbrido", "Apple Maps · Estándar (noche)"][sim.phase])
                .font(.system(size: 15, weight: .semibold)).padding(.horizontal, 14).padding(.vertical, 8)
                .background(.ultraThinMaterial, in: Capsule()).padding(.top, 60)
        }
        .task { await run() }
    }

    func place() {
        let here = sim.at(s)
        let target = sim.bearing(sim.at(s - 30), sim.at(s + 140))
        var h = head ?? target
        let diff = (target - h + 540).truncatingRemainder(dividingBy: 360) - 180
        h += diff * 0.15; head = h
        sim.pos = here
        // la cámara mira por delante del icono para dejarlo abajo en la pantalla
        let aerial = sim.phase == 1
        let ahead = sim.at(s + (aerial ? 900 : 70))
        withAnimation(.linear(duration: tick)) {
            cam = .camera(MapCamera(centerCoordinate: ahead, distance: aerial ? 3200 : 650, heading: h, pitch: aerial ? 72 : 68))
        }
    }

    func run() async {
        // calentar: dejar que cargue el primer tramo con cada estilo
        for ph in [0, 1, 2, 3, 0] { sim.phase = ph; s = Double(ph) * 1000; place(); try? await Task.sleep(nanoseconds: 3_000_000_000) }
        try? await Task.sleep(nanoseconds: 4_000_000_000)
        let per = 10.5   // segundos por estilo
        for ph in 0..<4 {
            sim.phase = ph
            let start = Double(ph) * 1000
            s = start
            let steps = Int(per / tick)
            for _ in 0..<steps {
                s += speed * tick
                place()
                try? await Task.sleep(nanoseconds: UInt64(tick * 1_000_000_000))
            }
        }
    }
}
