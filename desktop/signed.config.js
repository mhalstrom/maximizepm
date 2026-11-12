// The build of a signed and notarized app (#5615): desktop-release.yml uses it when the signing secrets exist
// (electron-builder --config signed.config.js). The settings in package.json build the unsigned app, signed ad hoc
// (identity "-"). Here electron-builder finds the Developer ID certificate that CSC_LINK holds, signs every program
// of the app with the hardened runtime and entitlements.mac.plist, signs the .dmg files, and notarizes and staples
// the app when APPLE_API_KEY (the path of the .p8 file), APPLE_API_KEY_ID and APPLE_API_ISSUER are set.
const base = require("./package.json").build;

const mac = { ...base.mac, hardenedRuntime: true };
delete mac.identity;  // not "-": the certificate of CSC_LINK

module.exports = { ...base, mac, dmg: { ...base.dmg, sign: true }, forceCodeSigning: true };
