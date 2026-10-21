// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "ADCControl",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .library(
            name: "ADCMonitorCore",
            targets: ["ADCMonitorCore"]
        ),
        .executable(
            name: "adcctl",
            targets: ["adcctl"]
        ),
        .executable(
            name: "ADCControlApp",
            targets: ["ADCControlApp"]
        ),
    ],
    targets: [
        .target(
            name: "ADCMonitorCore",
            linkerSettings: [
                .linkedFramework("CoreDisplay"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("Foundation"),
                .linkedFramework("IOKit"),
            ]
        ),
        .executableTarget(
            name: "adcctl",
            dependencies: ["ADCMonitorCore"]
        ),
        .executableTarget(
            name: "ADCControlApp",
            dependencies: ["ADCMonitorCore"],
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("SwiftUI"),
            ]
        ),
    ]
)
