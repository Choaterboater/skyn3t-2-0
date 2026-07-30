"""Rich iPhone-first Cellar Companion scaffold for Swift iOS wine briefs."""
from __future__ import annotations


def cellar_companion_files(
    title: str, bundle: str, brief: str, project_template: str,
) -> dict[str, str]:
    files = dict(_FILES)
    files["App.xcodeproj/project.pbxproj"] = project_template.replace("__BUNDLE__", bundle)
    files["README.md"] = _readme(title, brief)
    return files


_FILES: dict[str, str] = {
    "App/App.swift": r'''import SwiftUI
import SwiftData

@main
struct CellarCompanionApp: App {
    var body: some Scene {
        WindowGroup { RootView() }
            .modelContainer(for: [Bottle.self, TastingReview.self])
    }
}
''',
    "App/Models.swift": r'''import Foundation
import Observation
import SwiftData

enum WineStyle: String, CaseIterable, Identifiable {
    case red, white, rose, sparkling, orange, dessert, fortified, other
    var id: String { rawValue }
    var label: String {
        switch self {
        case .red: "Red"; case .white: "White"; case .rose: "Rosé"
        case .sparkling: "Sparkling"; case .orange: "Orange"
        case .dessert: "Dessert"; case .fortified: "Fortified"; case .other: "Other"
        }
    }
}

enum BottleStatus: String, CaseIterable, Identifiable {
    case cellared, drinkSoon, hold, opened, consumed
    var id: String { rawValue }
    var label: String {
        switch self {
        case .cellared: "In cellar"; case .drinkSoon: "Drink soon"
        case .hold: "Hold"; case .opened: "Opened"; case .consumed: "Consumed"
        }
    }
}

enum DrinkWindowState: Equatable { case drinkNow, hold, past, unknown }

enum ReviewOrigin: String, CaseIterable, Identifiable {
    case personal, person, provider
    var id: String { rawValue }
    var label: String {
        switch self {
        case .personal: "My tasting note"; case .person: "Someone I know"; case .provider: "Review provider"
        }
    }
}

@Model final class Bottle: Identifiable {
    @Attribute(.unique) var id: UUID
    var producer: String
    var wineName: String
    var vintage: Int?
    var varietal: String
    var region: String
    var country: String
    var appellation: String
    var styleRaw: String
    var bottleSizeML: Int
    var quantity: Int
    var purchasePrice: Double?
    var purchaseDate: Date?
    var purchaseSource: String
    var estimatedValue: Double?
    var currencyCode: String
    var valueSource: String
    var valueCheckedAt: Date?
    var valueConfidence: String
    var storageLocation: String
    var drinkWindowStart: Date?
    var drinkWindowEnd: Date?
    var statusRaw: String
    var isFavorite: Bool
    var isWishList: Bool
    var lowStockThreshold: Int
    var tastingRating: Double?
    var tastingNotes: String
    var foodPairings: String
    var privateNotes: String
    var barcode: String
    var scanText: String
    var createdAt: Date
    var updatedAt: Date

    init(producer: String = "", wineName: String = "", vintage: Int? = nil, quantity: Int = 1) {
        id = UUID()
        self.producer = producer; self.wineName = wineName; self.vintage = vintage
        varietal = ""; region = ""; country = ""; appellation = ""
        styleRaw = WineStyle.other.rawValue; bottleSizeML = 750; self.quantity = max(1, quantity)
        purchasePrice = nil; purchaseDate = nil; purchaseSource = ""
        estimatedValue = nil; currencyCode = "USD"; valueSource = ""; valueCheckedAt = nil; valueConfidence = ""
        storageLocation = ""; drinkWindowStart = nil; drinkWindowEnd = nil; statusRaw = BottleStatus.cellared.rawValue
        isFavorite = false; isWishList = false; lowStockThreshold = 1
        tastingRating = nil; tastingNotes = ""; foodPairings = ""; privateNotes = ""; barcode = ""; scanText = ""
        createdAt = .now; updatedAt = .now
    }

    var style: WineStyle { get { WineStyle(rawValue: styleRaw) ?? .other } set { styleRaw = newValue.rawValue } }
    var status: BottleStatus { get { BottleStatus(rawValue: statusRaw) ?? .cellared } set { statusRaw = newValue.rawValue } }
    var displayName: String { [producer.trimmed, wineName.trimmed].filter { !$0.isEmpty }.joined(separator: " — ") }
    var subtitle: String { [vintage.map(String.init) ?? "NV", varietal.trimmed, storageLocation.trimmed].filter { !$0.isEmpty }.joined(separator: " • ") }
    var searchText: String { [producer, wineName, vintage.map(String.init) ?? "", varietal, region, country, appellation, storageLocation, barcode].joined(separator: " ").folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current) }
    var lowStock: Bool { quantity <= max(0, lowStockThreshold) }
    var totalValue: Double? { estimatedValue.map { $0 * Double(quantity) } }
}

@Model final class TastingReview: Identifiable {
    @Attribute(.unique) var id: UUID
    var bottleID: UUID
    var author: String
    var rating: Double?
    var note: String
    var originRaw: String
    var sourceName: String
    var createdAt: Date

    init(bottleID: UUID, author: String, rating: Double?, note: String, origin: ReviewOrigin, sourceName: String = "") {
        id = UUID(); self.bottleID = bottleID; self.author = author; self.rating = rating
        self.note = note; originRaw = origin.rawValue; self.sourceName = sourceName; createdAt = .now
    }

    var origin: ReviewOrigin { ReviewOrigin(rawValue: originRaw) ?? .personal }
}

@Observable final class BottleForm {
    var producer = ""; var wineName = ""; var vintage = ""; var varietal = ""; var region = ""; var country = ""; var appellation = ""
    var styleRaw = WineStyle.other.rawValue; var bottleSizeML = 750; var quantity = 1
    var purchasePrice = ""; var hasPurchaseDate = false; var purchaseDate = Date(); var purchaseSource = ""
    var estimatedValue = ""; var currencyCode = "USD"; var valueSource = ""; var hasValueDate = false; var valueDate = Date(); var valueConfidence = ""
    var storageLocation = ""; var hasDrinkWindow = false; var drinkStart = Date(); var drinkEnd = Calendar.current.date(byAdding: .year, value: 3, to: .now) ?? .now
    var statusRaw = BottleStatus.cellared.rawValue; var isFavorite = false; var isWishList = false; var lowStockThreshold = 1
    var tastingRating = ""; var tastingNotes = ""; var foodPairings = ""; var privateNotes = ""; var barcode = ""; var scanText = ""

    init(bottle: Bottle? = nil) {
        guard let bottle else { return }
        producer = bottle.producer; wineName = bottle.wineName; vintage = bottle.vintage.map(String.init) ?? ""; varietal = bottle.varietal
        region = bottle.region; country = bottle.country; appellation = bottle.appellation; styleRaw = bottle.styleRaw
        bottleSizeML = bottle.bottleSizeML; quantity = bottle.quantity; purchasePrice = BottleForm.text(bottle.purchasePrice)
        hasPurchaseDate = bottle.purchaseDate != nil; purchaseDate = bottle.purchaseDate ?? .now; purchaseSource = bottle.purchaseSource
        estimatedValue = BottleForm.text(bottle.estimatedValue); currencyCode = bottle.currencyCode; valueSource = bottle.valueSource
        hasValueDate = bottle.valueCheckedAt != nil; valueDate = bottle.valueCheckedAt ?? .now; valueConfidence = bottle.valueConfidence
        storageLocation = bottle.storageLocation; hasDrinkWindow = bottle.drinkWindowStart != nil || bottle.drinkWindowEnd != nil
        drinkStart = bottle.drinkWindowStart ?? .now; drinkEnd = bottle.drinkWindowEnd ?? Calendar.current.date(byAdding: .year, value: 3, to: .now) ?? .now
        statusRaw = bottle.statusRaw; isFavorite = bottle.isFavorite; isWishList = bottle.isWishList; lowStockThreshold = bottle.lowStockThreshold
        tastingRating = BottleForm.text(bottle.tastingRating); tastingNotes = bottle.tastingNotes; foodPairings = bottle.foodPairings
        privateNotes = bottle.privateNotes; barcode = bottle.barcode; scanText = bottle.scanText
    }

    var lookupQuery: String { [producer, wineName, vintage].map(\.trimmed).filter { !$0.isEmpty }.joined(separator: " ") }
    var error: String? {
        if producer.trimmed.isEmpty && wineName.trimmed.isEmpty { return "Enter a producer or wine name." }
        if let value = number(purchasePrice), value < 0 { return "Purchase price cannot be negative." }
        if let value = number(estimatedValue), value < 0 { return "Estimated value cannot be negative." }
        if let rating = number(tastingRating), !(0...5).contains(rating) { return "Rating must be between 0 and 5." }
        if hasDrinkWindow && drinkEnd < drinkStart { return "Drink-window end must come after start." }
        return nil
    }

    func use(scan: BottleScanResult) {
        scanText = scan.rawText; if producer.trimmed.isEmpty { producer = scan.producer }
        if wineName.trimmed.isEmpty { wineName = scan.wineName }
        if vintage.trimmed.isEmpty, let vintage = scan.vintage { self.vintage = String(vintage) }
        if region.trimmed.isEmpty { region = scan.region }; if country.trimmed.isEmpty { country = scan.country }
        if barcode.trimmed.isEmpty { barcode = scan.barcode }
    }

    func apply(to bottle: Bottle) {
        bottle.producer = producer.trimmed; bottle.wineName = wineName.trimmed; bottle.vintage = Int(vintage.trimmed)
        bottle.varietal = varietal.trimmed; bottle.region = region.trimmed; bottle.country = country.trimmed; bottle.appellation = appellation.trimmed
        bottle.styleRaw = styleRaw; bottle.bottleSizeML = bottleSizeML; bottle.quantity = max(1, quantity)
        bottle.purchasePrice = number(purchasePrice); bottle.purchaseDate = hasPurchaseDate ? purchaseDate : nil; bottle.purchaseSource = purchaseSource.trimmed
        bottle.estimatedValue = number(estimatedValue); bottle.currencyCode = currencyCode.trimmed.isEmpty ? "USD" : currencyCode.trimmed.uppercased()
        bottle.valueSource = valueSource.trimmed; bottle.valueCheckedAt = hasValueDate ? valueDate : nil; bottle.valueConfidence = valueConfidence.trimmed
        bottle.storageLocation = storageLocation.trimmed; bottle.drinkWindowStart = hasDrinkWindow ? drinkStart : nil; bottle.drinkWindowEnd = hasDrinkWindow ? drinkEnd : nil
        bottle.statusRaw = statusRaw; bottle.isFavorite = isFavorite; bottle.isWishList = isWishList; bottle.lowStockThreshold = max(0, lowStockThreshold)
        bottle.tastingRating = number(tastingRating); bottle.tastingNotes = tastingNotes.trimmed; bottle.foodPairings = foodPairings.trimmed; bottle.privateNotes = privateNotes.trimmed
        bottle.barcode = barcode.trimmed; bottle.scanText = scanText.trimmed; bottle.updatedAt = .now
    }

    func makeBottle() -> Bottle { let bottle = Bottle(); apply(to: bottle); return bottle }
    private func number(_ value: String) -> Double? { let text = value.trimmed.replacingOccurrences(of: ",", with: ""); return text.isEmpty ? nil : Double(text) }
    private static func text(_ value: Double?) -> String { value.map { String(format: "%.2f", $0) } ?? "" }
}

struct BottleScanResult: Sendable {
    var rawText: String; var barcode: String; var producer: String; var wineName: String; var vintage: Int?; var region: String; var country: String
}

enum BottleTextParser {
    static func vintage(in text: String) -> Int? {
        let pattern = "(?<![0-9])(19[0-9]{2}|20[0-9]{2})(?![0-9])"
        guard let range = text.range(of: pattern, options: .regularExpression) else { return nil }
        return Int(text[range])
    }

    static func parse(text: String, barcode: String = "") -> BottleScanResult {
        let lines = text.split(whereSeparator: \.isNewline).map { clean(String($0)) }.filter { !$0.isEmpty }
        let countries = ["Argentina", "Australia", "Austria", "Chile", "France", "Germany", "Italy", "New Zealand", "Portugal", "South Africa", "Spain", "United States"]
        let regions = ["Bordeaux", "Burgundy", "Champagne", "Napa Valley", "Sonoma", "Tuscany", "Piedmont", "Rioja", "Mosel", "Barossa", "Marlborough"]
        return BottleScanResult(rawText: clean(text), barcode: barcode, producer: lines.first ?? "", wineName: lines.dropFirst().first ?? "", vintage: vintage(in: text), region: regions.first { text.localizedCaseInsensitiveContains($0) } ?? "", country: countries.first { text.localizedCaseInsensitiveContains($0) } ?? "")
    }

    static func clean(_ text: String) -> String { text.replacingOccurrences(of: "\n", with: " ").split(whereSeparator: \.isWhitespace).joined(separator: " ") }
}

enum Inventory {
    static func state(_ bottle: Bottle, now: Date = .now) -> DrinkWindowState {
        if bottle.status == .hold { return .hold }
        guard let start = bottle.drinkWindowStart, let end = bottle.drinkWindowEnd else { return .unknown }
        if now < start { return .hold }; if now > end { return .past }; return .drinkNow
    }

    static func bottleCount(_ bottles: [Bottle]) -> Int { bottles.reduce(0) { $0 + $1.quantity } }
    static func value(_ bottles: [Bottle]) -> Double { bottles.compactMap(\.totalValue).reduce(0, +) }
    static func csv(_ bottles: [Bottle]) -> String {
        let header = ["Producer", "Wine", "Vintage", "Quantity", "Location", "Purchase Price", "Estimated Value", "Currency", "Value Source", "Notes"]
        let rows = bottles.map { [$0.producer, $0.wineName, $0.vintage.map(String.init) ?? "", String($0.quantity), $0.storageLocation, $0.purchasePrice.map(String.init) ?? "", $0.estimatedValue.map(String.init) ?? "", $0.currencyCode, $0.valueSource, $0.privateNotes] }
        return ([header] + rows).map { $0.map { "\"\($0.replacingOccurrences(of: "\"", with: "\"\""))\"" }.joined(separator: ",") }.joined(separator: "\n")
    }
}

enum CurrencyText {
    static func value(_ amount: Double?, code: String) -> String { amount.map { $0.formatted(.currency(code: code.isEmpty ? "USD" : code)) } ?? "Not set" }
}

extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
    func ifEmpty(_ fallback: String) -> String { trimmed.isEmpty ? fallback : self }
}
''',
    "App/Providers.swift": r'''import Foundation
import SwiftUI

enum ReviewProvider: String, CaseIterable, Identifiable {
    case vivino, wineSearcher, wineEnthusiast, decanter
    var id: String { rawValue }
    var name: String {
        switch self {
        case .vivino: "Vivino"; case .wineSearcher: "Wine-Searcher"
        case .wineEnthusiast: "Wine Enthusiast"; case .decanter: "Decanter"
        }
    }

    func searchURL(for bottle: Bottle) -> URL {
        let query = [bottle.producer, bottle.wineName, bottle.vintage.map(String.init) ?? ""].filter { !$0.trimmed.isEmpty }.joined(separator: " ")
        let base: String
        switch self {
        case .vivino: base = "https://www.vivino.com/search/wines"
        case .wineSearcher: base = "https://www.wine-searcher.com/find"
        case .wineEnthusiast: base = "https://www.wineenthusiast.com/search"
        case .decanter: base = "https://www.decanter.com/search"
        }
        return ValueLinks.url(base, query)
    }
}

enum ValueLinks {
    static func url(_ base: String, _ query: String) -> URL {
        var components = URLComponents(string: base) ?? URLComponents()
        components.queryItems = [URLQueryItem(name: "q", value: query)]
        return components.url ?? URL(string: "https://www.apple.com")!
    }

    static func currentOffers(for query: String) -> [(String, URL)] {
        [
            ("Search Wine-Searcher offers", url("https://www.wine-searcher.com/find", query)),
            ("Search Vivino", url("https://www.vivino.com/search/wines", query))
        ]
    }
}

struct MarketValueRequest: Codable, Sendable {
    var producer: String; var wineName: String; var vintage: Int?; var bottleSizeML: Int; var country: String; var region: String
}

struct MarketValueQuote: Codable, Sendable {
    var amount: Double; var currencyCode: String; var sourceName: String; var asOf: Date; var comparableCount: Int?; var confidence: String
}

protocol MarketValueProvider: Sendable {
    func quote(for request: MarketValueRequest) async throws -> MarketValueQuote
}

/// Use this only with a backend or licensed provider you control. Vendor keys
/// belong in server-side or Keychain-managed configuration, never in source.
struct AuthorizedMarketValueProvider: MarketValueProvider {
    let endpoint: URL
    let bearerToken: String

    func quote(for request: MarketValueRequest) async throws -> MarketValueQuote {
        guard endpoint.scheme == "https" else { throw URLError(.appTransportSecurityRequiresSecureConnection) }
        var call = URLRequest(url: endpoint)
        call.httpMethod = "POST"; call.setValue("application/json", forHTTPHeaderField: "Content-Type")
        call.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        call.httpBody = try JSONEncoder().encode(request)
        let (data, response) = try await URLSession.shared.data(for: call)
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else { throw URLError(.badServerResponse) }
        return try JSONDecoder().decode(MarketValueQuote.self, from: data)
    }
}

struct ValueLookupView: View {
    @Environment(\.dismiss) private var dismiss
    let query: String
    let apply: (String, String, String, Date) -> Void
    @State private var amount = ""; @State private var source = "Manual estimate"; @State private var confidence = "Manual estimate"; @State private var checked = Date(); @State private var error = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Search current offers") {
                    Text("Compare exact vintage, size, provenance, condition, taxes, and currency. Asking prices are not guaranteed resale values.")
                        .font(.footnote).foregroundStyle(.secondary)
                    ForEach(ValueLinks.currentOffers(for: query), id: \.0) { item in
                        Link(destination: item.1) { Label(item.0, systemImage: "safari") }
                    }
                }
                Section("Record a careful estimate") {
                    TextField("Estimated value", text: $amount).keyboardType(.decimalPad)
                    TextField("Source or provider", text: $source)
                    TextField("Confidence", text: $confidence)
                    DatePicker("Checked on", selection: $checked, displayedComponents: .date)
                    Text("Automatic lookup needs a licensed HTTPS data source. AI may match a bottle to comparables, but it must show the source and never invent a price.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if !error.isEmpty { Text(error).foregroundStyle(.red) }
            }
            .navigationTitle("Check market value")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Use estimate") {
                        guard let value = Double(amount.replacingOccurrences(of: ",", with: "")), value >= 0 else { error = "Enter a zero or positive estimated value."; return }
                        apply(String(format: "%.2f", value), source.trimmed, confidence.trimmed, checked); dismiss()
                    }
                }
            }
        }
    }
}
''',
    "App/BottleScannerView.swift": r'''import SwiftUI
import VisionKit

struct ScannerSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""; @State private var barcode = ""
    let capture: (BottleScanResult) -> Void

    var body: some View {
        NavigationStack {
            VStack(spacing: 14) {
                if DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                    BottleScannerView(text: $text, barcode: $barcode).clipShape(RoundedRectangle(cornerRadius: 20)).frame(maxHeight: 300)
                } else {
                    ContentUnavailableView("Scanner unavailable", systemImage: "camera.slash", description: Text("Use the complete manual form below."))
                }
                Form {
                    Section("Review and correct") {
                        TextField("Recognized label text", text: $text, axis: .vertical).lineLimit(3...6)
                        TextField("Recognized barcode", text: $barcode).keyboardType(.numberPad)
                        Text("Nothing is saved until you review these suggestions in the bottle form.").font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Scan bottle")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Use details") { capture(BottleTextParser.parse(text: text, barcode: barcode)); dismiss() }
                        .disabled(text.trimmed.isEmpty && barcode.trimmed.isEmpty)
                }
            }
        }
    }
}

struct BottleScannerView: UIViewControllerRepresentable {
    @Binding var text: String; @Binding var barcode: String
    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(recognizedDataTypes: [.barcode(symbologies: [.ean13, .ean8, .upce, .code128]), .text(languages: ["en"], textContentType: nil)], qualityLevel: .balanced, recognizesMultipleItems: true, isHighFrameRateTrackingEnabled: false, isPinchToZoomEnabled: true, isGuidanceEnabled: true, isHighlightingEnabled: true)
        scanner.delegate = context.coordinator; try? scanner.startScanning(); return scanner
    }

    func updateUIViewController(_ uiViewController: DataScannerViewController, context: Context) {}
    static func dismantleUIViewController(_ uiViewController: DataScannerViewController, coordinator: Coordinator) { uiViewController.stopScanning() }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        var parent: BottleScannerView
        init(parent: BottleScannerView) { self.parent = parent }
        func dataScanner(_ dataScanner: DataScannerViewController, didAdd items: [RecognizedItem], allItems: [RecognizedItem]) {
            for item in items {
                switch item {
                case .barcode(let item): if parent.barcode.isEmpty { parent.barcode = item.payloadStringValue ?? "" }
                case .text(let item): if parent.text.count < item.transcript.count { parent.text = item.transcript }
                @unknown default: break
                }
            }
        }
    }
}
''',
    "App/ContentView.swift": r'''import SwiftUI
import SwiftData

enum CellarTheme { static let wine = Color(red: 0.43, green: 0.08, blue: 0.14) }

struct RootView: View {
    var body: some View {
        TabView {
            NavigationStack { HomeView() }.tabItem { Label("Home", systemImage: "house.fill") }
            NavigationStack { CellarView() }.tabItem { Label("Cellar", systemImage: "wineglass") }
            NavigationStack { AddBottleLauncher() }.tabItem { Label("Add", systemImage: "plus.circle.fill") }
            NavigationStack { SettingsView() }.tabItem { Label("Settings", systemImage: "gearshape") }
        }.tint(CellarTheme.wine)
    }
}

struct HomeView: View {
    @Environment(\.modelContext) private var context
    @Query(sort: \Bottle.updatedAt, order: .reverse) private var bottles: [Bottle]
    @State private var showAdd = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Your collection, at a glance").font(.title2.bold())
                Text("A calm, iPhone-first view of what you have and what needs attention.").foregroundStyle(.secondary)
                Button { showAdd = true } label: { Label("Add a bottle", systemImage: "plus.circle.fill").frame(maxWidth: .infinity) }
                    .buttonStyle(.borderedProminent).controlSize(.large)
                if bottles.isEmpty {
                    ContentUnavailableView("Your cellar is ready", systemImage: "wineglass", description: Text("Start with one bottle or load sample data to explore the app."))
                    Button("Load sample cellar") { Samples.insert(into: context, bottles: bottles) }.buttonStyle(.bordered).controlSize(.large)
                } else {
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                        Metric(title: "Bottles", value: "\(Inventory.bottleCount(bottles))", image: "wineglass.fill")
                        Metric(title: "Wines", value: "\(bottles.count)", image: "square.grid.2x2.fill")
                        Metric(title: "Collection value", value: CurrencyText.value(Inventory.value(bottles), code: bottles.first?.currencyCode ?? "USD"), image: "banknote")
                        Metric(title: "Low stock", value: "\(bottles.filter(\.lowStock).count)", image: "exclamationmark.triangle.fill")
                    }
                    BottleSection("Drink now", subtitle: "Based on your drink windows", bottles: bottles.filter { Inventory.state($0) == .drinkNow })
                    BottleSection("Low stock", subtitle: "At or below your alert threshold", bottles: bottles.filter(\.lowStock))
                    BottleSection("Recently added", subtitle: "Newest bottles", bottles: Array(bottles.prefix(5)))
                }
            }.padding()
        }
        .navigationTitle("Cellar Companion")
        .sheet(isPresented: $showAdd) { NavigationStack { BottleEditor() } }
    }
}

struct Metric: View {
    let title: String; let value: String; let image: String
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Image(systemName: image).foregroundStyle(CellarTheme.wine)
            Text(value).font(.title3.bold()).lineLimit(1).minimumScaleFactor(0.7)
            Text(title).font(.footnote).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity, minHeight: 105, alignment: .leading).padding().background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18))
    }
}

struct BottleSection: View {
    let title: String; let subtitle: String; let bottles: [Bottle]
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.title3.bold()); Text(subtitle).font(.footnote).foregroundStyle(.secondary)
            if bottles.isEmpty { Text("Nothing to show yet.").foregroundStyle(.secondary) }
            ForEach(bottles.prefix(5)) { bottle in NavigationLink { BottleDetail(bottle: bottle) } label: { BottleRow(bottle: bottle) }.buttonStyle(.plain) }
        }.padding().background(.background, in: RoundedRectangle(cornerRadius: 18)).overlay { RoundedRectangle(cornerRadius: 18).stroke(.quaternary) }
    }
}

enum CellarFilter: String, CaseIterable, Identifiable {
    case all, drinkNow, hold, lowStock, favorites, wishList, red, white, sparkling
    var id: String { rawValue }
    var label: String { switch self { case .all: "All bottles"; case .drinkNow: "Drink now"; case .hold: "Hold"; case .lowStock: "Low stock"; case .favorites: "Favorites"; case .wishList: "Wish list"; case .red: "Red wine"; case .white: "White wine"; case .sparkling: "Sparkling" } }
    func matches(_ bottle: Bottle) -> Bool {
        switch self {
        case .all: true; case .drinkNow: Inventory.state(bottle) == .drinkNow; case .hold: Inventory.state(bottle) == .hold
        case .lowStock: bottle.lowStock; case .favorites: bottle.isFavorite; case .wishList: bottle.isWishList
        case .red: bottle.style == .red; case .white: bottle.style == .white; case .sparkling: bottle.style == .sparkling
        }
    }
}

enum CellarSort: String, CaseIterable, Identifiable {
    case recent, wine, producer, vintage, quantity, purchasePrice, estimatedValue
    var id: String { rawValue }
    var label: String { switch self { case .recent: "Recently added"; case .wine: "Wine name"; case .producer: "Producer"; case .vintage: "Vintage"; case .quantity: "Quantity"; case .purchasePrice: "Purchase price"; case .estimatedValue: "Estimated value" } }
    func apply(_ bottles: [Bottle]) -> [Bottle] {
        switch self {
        case .recent: bottles.sorted { $0.createdAt > $1.createdAt }
        case .wine: bottles.sorted { $0.wineName.localizedCaseInsensitiveCompare($1.wineName) == .orderedAscending }
        case .producer: bottles.sorted { $0.producer.localizedCaseInsensitiveCompare($1.producer) == .orderedAscending }
        case .vintage: bottles.sorted { ($0.vintage ?? 0) > ($1.vintage ?? 0) }
        case .quantity: bottles.sorted { $0.quantity > $1.quantity }
        case .purchasePrice: bottles.sorted { ($0.purchasePrice ?? -1) > ($1.purchasePrice ?? -1) }
        case .estimatedValue: bottles.sorted { ($0.estimatedValue ?? -1) > ($1.estimatedValue ?? -1) }
        }
    }
}

struct CellarView: View {
    @Query(sort: \Bottle.updatedAt, order: .reverse) private var bottles: [Bottle]
    @State private var query = ""; @State private var filter: CellarFilter = .all; @State private var sort: CellarSort = .recent; @State private var add = false
    var shown: [Bottle] {
        let text = query.trimmed.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
        return sort.apply(bottles.filter { (text.isEmpty || $0.searchText.contains(text)) && filter.matches($0) })
    }
    var body: some View {
        Group {
            if shown.isEmpty { ContentUnavailableView(query.trimmed.isEmpty ? "No bottles here yet" : "No bottles found", systemImage: query.trimmed.isEmpty ? "wineglass" : "magnifyingglass", description: Text("Search by wine, vintage, region, or rack; or add a bottle.")) }
            else { List(shown) { bottle in NavigationLink { BottleDetail(bottle: bottle) } label: { BottleRow(bottle: bottle) }.swipeActions { Button { bottle.quantity += 1; bottle.updatedAt = .now } label: { Label("Add one", systemImage: "plus") }.tint(.green) } }.listStyle(.plain) }
        }
        .navigationTitle("My Cellar").searchable(text: $query, prompt: "Search wine, vintage, region, or rack")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { Menu { Picker("Filter", selection: $filter) { ForEach(CellarFilter.allCases) { Text($0.label).tag($0) } } } label: { Label(filter.label, systemImage: "line.3.horizontal.decrease.circle") } }
            ToolbarItem(placement: .topBarTrailing) { Menu { Picker("Sort", selection: $sort) { ForEach(CellarSort.allCases) { Text($0.label).tag($0) } } } label: { Label("Sort", systemImage: "arrow.up.arrow.down") } }
            ToolbarItem(placement: .primaryAction) { Button { add = true } label: { Label("Add bottle", systemImage: "plus.circle.fill") } }
        }
        .sheet(isPresented: $add) { NavigationStack { BottleEditor() } }
    }
}

struct BottleRow: View {
    let bottle: Bottle
    var body: some View {
        HStack(spacing: 13) {
            Image(systemName: bottle.style == .white ? "wineglass" : "wineglass.fill").foregroundStyle(CellarTheme.wine).frame(width: 30)
            VStack(alignment: .leading, spacing: 3) {
                Text(bottle.displayName.ifEmpty("Unnamed bottle")).font(.headline).lineLimit(2)
                Text(bottle.subtitle.ifEmpty(bottle.status.label)).font(.subheadline).foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    if bottle.isFavorite { Label("Favorite", systemImage: "heart.fill").foregroundStyle(.pink) }
                    if bottle.lowStock { Label("Low", systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange) }
                }.font(.caption)
            }
            Spacer()
            VStack(alignment: .trailing) { Text("×\(bottle.quantity)").font(.headline.monospacedDigit()); Text(CurrencyText.value(bottle.estimatedValue, code: bottle.currencyCode)).font(.caption).foregroundStyle(.secondary) }
        }.padding(.vertical, 6).accessibilityElement(children: .combine).accessibilityHint("Open bottle details")
    }
}

struct AddBottleLauncher: View {
    var body: some View {
        VStack(spacing: 22) {
            Image(systemName: "plus.circle.fill").font(.system(size: 54)).foregroundStyle(CellarTheme.wine)
            Text("Add a bottle").font(.title.bold())
            Text("Scan a label or barcode, or type every detail by hand. Every scan remains editable before saving.").multilineTextAlignment(.center).foregroundStyle(.secondary)
            NavigationLink { BottleEditor() } label: { Label("Enter bottle details", systemImage: "pencil.and.list.clipboard").frame(maxWidth: .infinity) }.buttonStyle(.borderedProminent).controlSize(.large)
        }.padding().navigationTitle("Add")
    }
}
''',
    "App/BottleDetailView.swift": r'''import SwiftUI
import SwiftData

struct BottleDetail: View {
    @Environment(\.modelContext) private var context
    @Query(sort: \TastingReview.createdAt, order: .reverse) private var reviews: [TastingReview]
    let bottle: Bottle
    @State private var edit = false; @State private var review = false; @State private var value = false; @State private var consume = false; @State private var delete = false

    var bottleReviews: [TastingReview] { reviews.filter { $0.bottleID == bottle.id } }

    var body: some View {
        List {
            Section("At a glance") {
                LabeledContent("Producer", value: bottle.producer.ifEmpty("Not set")); LabeledContent("Wine", value: bottle.wineName.ifEmpty("Not set"))
                LabeledContent("Vintage", value: bottle.vintage.map(String.init) ?? "Non-vintage"); LabeledContent("Style", value: bottle.style.label)
                LabeledContent("Quantity", value: "\(bottle.quantity)"); LabeledContent("Location", value: bottle.storageLocation.ifEmpty("Not set")); LabeledContent("Status", value: bottle.status.label)
            }
            Section("Bottle details") {
                LabeledContent("Varietal", value: bottle.varietal.ifEmpty("Not set")); LabeledContent("Region", value: bottle.region.ifEmpty("Not set"))
                LabeledContent("Country", value: bottle.country.ifEmpty("Not set")); LabeledContent("Appellation", value: bottle.appellation.ifEmpty("Not set"))
                LabeledContent("Bottle size", value: "\(bottle.bottleSizeML) ml")
            }
            Section("Purchase and value") {
                LabeledContent("Purchase price", value: CurrencyText.value(bottle.purchasePrice, code: bottle.currencyCode)); LabeledContent("Purchased from", value: bottle.purchaseSource.ifEmpty("Not set"))
                LabeledContent("Current estimate", value: CurrencyText.value(bottle.estimatedValue, code: bottle.currencyCode)); LabeledContent("Collection estimate", value: CurrencyText.value(bottle.totalValue, code: bottle.currencyCode))
                LabeledContent("Source", value: bottle.valueSource.ifEmpty("Not set"))
                if let checked = bottle.valueCheckedAt { LabeledContent("Checked", value: checked.formatted(date: .abbreviated, time: .omitted)) }
                if !bottle.valueConfidence.isEmpty { LabeledContent("Confidence", value: bottle.valueConfidence) }
                Button { value = true } label: { Label("Check market value", systemImage: "magnifyingglass") }
            }
            Section("Your notes") {
                LabeledContent("Rating", value: bottle.tastingRating.map { String(format: "%.1f / 5", $0) } ?? "Not rated")
                Text(bottle.tastingNotes.ifEmpty("No tasting note yet."))
                if !bottle.foodPairings.isEmpty { LabeledContent("Food pairings", value: bottle.foodPairings) }
                if !bottle.privateNotes.isEmpty { VStack(alignment: .leading) { Text("Private notes").font(.subheadline.bold()); Text(bottle.privateNotes) } }
            }
            Section("People reviews") {
                Text("Personal and people-you-know notes stay in this private cellar. Website content remains attributed links.").font(.footnote).foregroundStyle(.secondary)
                if bottleReviews.isEmpty { Text("No people reviews yet.").foregroundStyle(.secondary) }
                ForEach(bottleReviews) { item in
                    VStack(alignment: .leading) {
                        HStack {
                            Text(item.author.ifEmpty("Anonymous")).font(.headline)
                            Spacer()
                            if let rating = item.rating { Text(String(format: "%.1f / 5", rating)).foregroundStyle(CellarTheme.wine) }
                        }
                        Text(item.note)
                        Text(item.origin.label + (item.sourceName.isEmpty ? "" : " • \(item.sourceName)")).font(.caption).foregroundStyle(.secondary)
                    }.padding(.vertical, 4)
                }
                Button { review = true } label: { Label("Add a people review", systemImage: "person.badge.plus") }
            }
            Section("External review sources") {
                Text("Open providers directly to see current content. The app does not scrape, copy, or present their ratings as its own.").font(.footnote).foregroundStyle(.secondary)
                ForEach(ReviewProvider.allCases) { provider in
                    Link(destination: provider.searchURL(for: bottle)) { Label("Search \(provider.name)", systemImage: "safari") }
                        .accessibilityHint("Opens \(provider.name) in Safari")
                }
            }
            Section("Inventory actions") {
                Button { consume = true } label: { Label("Drink one bottle", systemImage: "wineglass.fill") }
                Button(role: .destructive) { delete = true } label: { Label("Remove this bottle", systemImage: "trash") }
            }
        }
        .navigationTitle(bottle.displayName.ifEmpty("Bottle")).navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .primaryAction) { Button("Edit") { edit = true } } }
        .sheet(isPresented: $edit) { NavigationStack { BottleEditor(bottle: bottle) } }
        .sheet(isPresented: $review) { NavigationStack { ReviewEditor(bottle: bottle) } }
        .sheet(isPresented: $value) {
            ValueLookupView(query: [bottle.producer, bottle.wineName, bottle.vintage.map(String.init) ?? ""].filter { !$0.trimmed.isEmpty }.joined(separator: " ")) { amount, source, confidence, date in
                bottle.estimatedValue = Double(amount); bottle.valueSource = source; bottle.valueConfidence = confidence
                bottle.valueCheckedAt = date; bottle.updatedAt = .now; try? context.save()
            }
        }
        .confirmationDialog(bottle.quantity == 1 ? "Drink the last bottle?" : "Drink one bottle?", isPresented: $consume, titleVisibility: .visible) {
            Button("Confirm drink", role: .destructive) {
                if bottle.quantity > 1 { bottle.quantity -= 1; bottle.updatedAt = .now } else { context.delete(bottle) }
                try? context.save()
            }
        } message: {
            Text(bottle.quantity == 1 ? "This removes the last bottle from inventory." : "Quantity changes from \(bottle.quantity) to \(bottle.quantity - 1).")
        }
        .confirmationDialog("Remove this bottle?", isPresented: $delete, titleVisibility: .visible) {
            Button("Remove bottle", role: .destructive) { context.delete(bottle); try? context.save() }
        } message: { Text("This cannot be undone.") }
    }
}

struct ReviewEditor: View {
    @Environment(\.dismiss) private var dismiss; @Environment(\.modelContext) private var context
    let bottle: Bottle
    @State private var author = "Me"; @State private var rating = 4.0; @State private var note = ""; @State private var origin: ReviewOrigin = .personal; @State private var source = ""

    var body: some View {
        Form {
            TextField("Name", text: $author)
            Picker("Review type", selection: $origin) { ForEach(ReviewOrigin.allCases) { Text($0.label).tag($0) } }
            Slider(value: $rating, in: 0...5, step: 0.5) { Text("Rating") } minimumValueLabel: { Text("0") } maximumValueLabel: { Text("5") }
            Text("Rating: \(rating, specifier: "%.1f") / 5").foregroundStyle(.secondary)
            TextField("Review or tasting note", text: $note, axis: .vertical).lineLimit(3...8)
            if origin == .provider { TextField("Provider name", text: $source) }
        }.navigationTitle("Add review").toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) {
                Button("Save") {
                    context.insert(TastingReview(bottleID: bottle.id, author: author.trimmed, rating: rating, note: note.trimmed, origin: origin, sourceName: source.trimmed))
                    try? context.save(); dismiss()
                }.disabled(note.trimmed.isEmpty)
            }
        }
    }
}
''',
    "App/BottleEditorView.swift": r'''import SwiftUI
import SwiftData

struct BottleEditor: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.modelContext) private var context
    private let existing: Bottle?
    @State private var form: BottleForm
    @State private var scanner = false
    @State private var value = false
    @State private var validation = ""
    @State private var saveError = ""

    init(bottle: Bottle? = nil) {
        existing = bottle
        _form = State(initialValue: BottleForm(bottle: bottle))
    }

    var body: some View {
        @Bindable var form = form

        Form {
            Section("Start here") {
                Button { scanner = true } label: { Label("Scan bottle", systemImage: "viewfinder") }
                    .controlSize(.large)
                    .accessibilityHint("Reads a barcode and label text, then lets you review each suggested field.")
                Text("Manual entry is always available. Scanning only suggests fields that you can review and correct.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Section("Bottle identity") {
                TextField("Producer", text: $form.producer)
                TextField("Wine name", text: $form.wineName)
                TextField("Vintage, for example 2018", text: $form.vintage).keyboardType(.numberPad)
                TextField("Varietal or blend", text: $form.varietal)
                TextField("Appellation", text: $form.appellation)
                TextField("Region", text: $form.region)
                TextField("Country", text: $form.country)
                Picker("Style", selection: $form.styleRaw) {
                    ForEach(WineStyle.allCases) { Text($0.label).tag($0.rawValue) }
                }
            }
            Section("Inventory") {
                Stepper("Quantity: \(form.quantity)", value: $form.quantity, in: 1...999)
                Stepper("Bottle size: \(form.bottleSizeML) ml", value: $form.bottleSizeML, in: 187...3_000, step: 63)
                TextField("Cellar rack or bin", text: $form.storageLocation)
                Picker("Status", selection: $form.statusRaw) {
                    ForEach(BottleStatus.allCases) { Text($0.label).tag($0.rawValue) }
                }
                Toggle("Favorite", isOn: $form.isFavorite)
                Toggle("Wish list", isOn: $form.isWishList)
                Stepper("Low-stock alert at \(form.lowStockThreshold)", value: $form.lowStockThreshold, in: 0...99)
            }
            Section("Drink window") {
                Toggle("Set a drink window", isOn: $form.hasDrinkWindow)
                if form.hasDrinkWindow {
                    DatePicker("Start", selection: $form.drinkStart, displayedComponents: .date)
                    DatePicker("End", selection: $form.drinkEnd, displayedComponents: .date)
                }
            }
            Section("Purchase") {
                TextField("Purchase price", text: $form.purchasePrice).keyboardType(.decimalPad)
                Toggle("Remember purchase date", isOn: $form.hasPurchaseDate)
                if form.hasPurchaseDate { DatePicker("Purchase date", selection: $form.purchaseDate, displayedComponents: .date) }
                TextField("Purchased from", text: $form.purchaseSource)
            }
            Section("Value") {
                TextField("Currency code, for example USD", text: $form.currencyCode).textInputAutocapitalization(.characters).autocorrectionDisabled()
                TextField("Estimated value", text: $form.estimatedValue).keyboardType(.decimalPad)
                TextField("Value source", text: $form.valueSource)
                TextField("Value confidence", text: $form.valueConfidence)
                Toggle("Remember value check date", isOn: $form.hasValueDate)
                if form.hasValueDate { DatePicker("Value checked", selection: $form.valueDate, displayedComponents: .date) }
                Button { value = true } label: { Label("Search current value", systemImage: "magnifyingglass") }
            }
            Section("Your notes") {
                TextField("Personal rating from 0 to 5", text: $form.tastingRating).keyboardType(.decimalPad)
                TextField("Tasting notes", text: $form.tastingNotes, axis: .vertical).lineLimit(3...7)
                TextField("Food pairings", text: $form.foodPairings, axis: .vertical)
                TextField("Private notes", text: $form.privateNotes, axis: .vertical).lineLimit(3...7)
            }
            Section("Scan record") {
                TextField("Barcode", text: $form.barcode).keyboardType(.numberPad)
                if !form.scanText.trimmed.isEmpty { Text(form.scanText).font(.footnote).foregroundStyle(.secondary) }
            }
            if !validation.isEmpty { Text(validation).foregroundStyle(.red) }
            if !saveError.isEmpty { Text(saveError).foregroundStyle(.red) }
        }
        .navigationTitle(existing == nil ? "Add bottle" : "Edit bottle")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) { Button(existing == nil ? "Save" : "Save changes") { save() } }
        }
        .sheet(isPresented: $scanner) { ScannerSheet { form.use(scan: $0) } }
        .sheet(isPresented: $value) {
            ValueLookupView(query: form.lookupQuery) { amount, source, confidence, date in
                form.estimatedValue = amount; form.valueSource = source; form.valueConfidence = confidence
                form.hasValueDate = true; form.valueDate = date
            }
        }
    }

    private func save() {
        validation = form.error ?? ""
        guard validation.isEmpty else { return }
        if let existing { form.apply(to: existing) } else { context.insert(form.makeBottle()) }
        do { try context.save(); dismiss() } catch { saveError = "Could not save this bottle. Please try again." }
    }
}

struct SettingsView: View {
    @Query(sort: \Bottle.updatedAt, order: .reverse) private var bottles: [Bottle]

    var body: some View {
        List {
            Section("Your data") {
                ShareLink(item: Inventory.csv(bottles), preview: SharePreview("Cellar Companion inventory.csv")) {
                    Label("Export inventory as CSV", systemImage: "square.and.arrow.up")
                }
                Text("Export is a local snapshot. Import and cloud sync are deliberately future, opt-in seams until you choose a backup provider.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Section("Market-value sources") {
                Label("Use current asking prices carefully", systemImage: "chart.line.uptrend.xyaxis")
                Text("Exact vintage, bottle size, provenance, storage, taxes, and currency matter. Each estimate records a source and date. Automatic quotes need a licensed HTTPS provider; never hard-code keys or scrape a review website.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Section("Accessibility") {
                Label("Large native controls", systemImage: "hand.tap")
                Label("Dynamic Type and VoiceOver labels", systemImage: "textformat.size")
                Label("Manual entry without a camera", systemImage: "pencil.line")
            }
            Section("About") {
                LabeledContent("App", value: "Cellar Companion")
                LabeledContent("Design", value: "iPhone-first, offline-first")
            }
        }
        .navigationTitle("Settings")
    }
}
''',
    "App/SampleData.swift": r'''import SwiftData

enum Samples {
    static func insert(into context: ModelContext, bottles: [Bottle]) {
        guard bottles.isEmpty else { return }
        let red = Bottle(producer: "Cascina Example", wineName: "Barolo", vintage: 2018, quantity: 3)
        red.varietal = "Nebbiolo"; red.region = "Piedmont"; red.country = "Italy"; red.style = .red; red.storageLocation = "Rack A • Bin 4"
        red.purchasePrice = 62; red.estimatedValue = 78; red.valueSource = "Manual sample estimate"; red.valueCheckedAt = .now; red.valueConfidence = "Sample only"
        red.drinkWindowStart = Calendar.current.date(byAdding: .year, value: 1, to: .now); red.drinkWindowEnd = Calendar.current.date(byAdding: .year, value: 8, to: .now)
        red.tastingNotes = "Rose, tar, cherry, and firm tannin."
        let white = Bottle(producer: "Coastal Example", wineName: "Chardonnay", vintage: 2022, quantity: 1)
        white.varietal = "Chardonnay"; white.region = "Sonoma Coast"; white.country = "United States"; white.style = .white; white.storageLocation = "Rack C • Bin 2"
        white.purchasePrice = 28; white.lowStockThreshold = 1; white.status = .drinkSoon; white.drinkWindowStart = .now; white.drinkWindowEnd = Calendar.current.date(byAdding: .year, value: 2, to: .now)
        context.insert(red); context.insert(white); try? context.save()
    }
}
''',
    "App/Info.plist": r'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleDisplayName</key><string>Cellar Companion</string>
<key>CFBundleIdentifier</key><string>$(PRODUCT_BUNDLE_IDENTIFIER)</string>
<key>CFBundleShortVersionString</key><string>$(MARKETING_VERSION)</string>
<key>CFBundleVersion</key><string>$(CURRENT_PROJECT_VERSION)</string>
<key>NSCameraUsageDescription</key><string>Cellar Companion uses the camera to scan bottle barcodes and label text so you can review and add wine more easily.</string>
<key>UILaunchScreen</key><dict/>
</dict></plist>
''',
    "App/Assets.xcassets/Contents.json": r'''{"info":{"author":"xcode","version":1}}''',
    "AppTests/AppTests.swift": r'''import XCTest
@testable import App

final class CellarCompanionTests: XCTestCase {
    func testVintageParser() { XCTAssertEqual(BottleTextParser.vintage(in: "Reserve 2018"), 2018) }

    func testCollectionValueUsesQuantity() {
        let bottle = Bottle(producer: "Example", wineName: "Red", vintage: 2020, quantity: 3)
        bottle.estimatedValue = 25
        XCTAssertEqual(Inventory.value([bottle]), 75)
    }

    func testDrinkWindow() {
        let bottle = Bottle(producer: "Example", wineName: "White")
        bottle.drinkWindowStart = Calendar.current.date(byAdding: .day, value: -1, to: .now)
        bottle.drinkWindowEnd = Calendar.current.date(byAdding: .day, value: 30, to: .now)
        XCTAssertEqual(Inventory.state(bottle), .drinkNow)
    }

    func testProviderLinksAreSecure() {
        let bottle = Bottle(producer: "Example", wineName: "Wine", vintage: 2020)
        XCTAssertTrue(ReviewProvider.allCases.allSatisfy { $0.searchURL(for: bottle).scheme == "https" })
        XCTAssertTrue(ValueLinks.currentOffers(for: "Example Wine").allSatisfy { $0.1.scheme == "https" })
    }
}
''',
    ".gitignore": "DerivedData/\nbuild/\nxcuserdata/\n*.xcuserstate\n.DS_Store\n",
}


def _readme(title: str, brief: str) -> str:
    return f'''# {title}

A native, iPhone-first SwiftUI wine-cellar app generated by SkyN3t.

## Features

- Offline-first SwiftData inventory for bottles, vintages, quantities, locations, purchase details, personal notes, drink windows, favorites, wish-list flags, and low-stock alerts
- Large labelled native controls, Dynamic Type, VoiceOver labels, high-contrast semantic controls, and a manual workflow that does not require a camera
- VisionKit barcode and label-text scanning with a review-and-correct screen before saving
- Personal and people-you-know tasting reviews stored locally, plus attributed external links to Vivino, Wine-Searcher, Wine Enthusiast, and Decanter
- Purchase price and a clearly sourced current-value estimate, including check date, currency, confidence, and collection-value totals
- Safe external price searches now, plus a narrow HTTPS market-value-provider integration seam for a licensed provider or backend you control
- CSV export, sample cellar data, quick consume action, search, filters, sorting, and deterministic inventory calculations

## Market-value policy

An asking price is not a guaranteed resale value. Compare exact producer, wine, vintage, bottle size, provenance, storage condition, taxes, and currency before recording an estimate. The app does not scrape websites. Automatic quotes should use a licensed provider through a backend you control; keep vendor keys off-device. AI may help match bottles to comparable offers, but it must show the source and must not invent a price.

## Build on a Mac

macOS and current Xcode are required to compile, run a Simulator, install on an iPhone, code-sign, and distribute the app.

1. Open App.xcodeproj in Xcode.
2. Select your Apple Development team under Signing and Capabilities.
3. Choose an installed iPhone Simulator or trusted iPhone.
4. Build and run.

Mac Terminal commands:

    xcodebuild -project App.xcodeproj -scheme App -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' build
    xcodebuild -project App.xcodeproj -scheme App -destination 'platform=iOS Simulator,name=<installed simulator>' test

For a physical iPhone, select your development team in Xcode and press Run. For TestFlight, archive in Xcode and distribute through App Store Connect.

## Project layout

- App/App.swift: SwiftUI entry and SwiftData container
- App/Models.swift: persistent bottle, review, form, parser, and inventory logic
- App/ContentView.swift and App/BottleDetailView.swift: iPhone-first dashboard, cellar, detail, and add/edit experience
- App/BottleScannerView.swift: VisionKit scanner and manual fallback
- App/Providers.swift: attributed review links and transparent licensed-value-provider seam
- AppTests/AppTests.swift: parser, inventory, drink-window, and HTTPS-link tests

## Original build brief

{brief}
'''
