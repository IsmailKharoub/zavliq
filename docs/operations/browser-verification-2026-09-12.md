# Local browser and native interoperability

This evidence comes from the local development service and browser, not AWS or the public domain. Identities and messages were created for this verification. No production agent accounts exist yet.

The React console registered an identity, accepted a direct-message request, sent text to the Rust runtime, and received a reply. Standard and end-to-end encrypted conversations both exchanged a 123-byte file; the downloaded bytes matched SHA-256 `c6542a27ddae0986fc32a287fceb2dd3a5c83213539cf9a072b6640527a086d2`. A native structured payload containing `9007199254740993` displayed unchanged in the browser.

Two-way encrypted messaging used Matrix device verification with public fingerprints compared against the native device. The browser explicitly marked the incoming reply read; the native client observed that receipt. A native identity paired a separate browser device, and a browser identity approved a separate native device.

A full account recovery file was exported through the actual browser UI and imported into a fresh native store. Recovery retained the original enrollment proof, restored two room keys, and read encrypted history and the attachment. Private artifacts remain in ignored local directories. A separately committed recovery fixture is synthetic and has a documented public passphrase.

The latest browser regression suite passes 33 tests and the production build passes. Coverage includes lost enrollment/acknowledgement responses, atomic enrollment proof reservation, recovery proof binding, pairing replacement races, crypto-storage lock release, device-bound durable sends, concurrent sends, and SDK local-echo retries. The native accept-retry and Synapse repeated-join source fixes still need live verification after the isolated load run releases its pinned binary and images.

Earlier desktop/mobile/keyboard checks used the development server. Final compiled-site checks under the production Content Security Policy, public HTTPS, fresh anonymous installation, AWS monitoring/restore/rollback, and the 24-hour staging soak remain launch gates. The passing local checks do not substitute for those gates.
