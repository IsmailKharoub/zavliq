/// <reference types="node" />
import { expect, it } from "vitest";
import { Decrypter } from "age-encryption";
import {
  initAsync,
  OlmMachine,
  UserId,
  DeviceId,
  RoomId,
  EncryptionSettings,
} from "@matrix-org/matrix-sdk-crypto-wasm";
import { encryptAccountBackup } from "./account-backup";
import { decryptRoomKeyFile } from "./room-key-file";
import { writeFileSync } from "node:fs";

it("exports the native recovery schema with age and standard Matrix room keys", async () => {
  await initAsync();
  const machine = await OlmMachine.initialize(
    new UserId("@fixture:localhost"),
    new DeviceId("FIXTURE"),
  );
  const password = "public interoperability fixture password";
  try {
    await machine.shareRoomKey(
      new RoomId("!interop:localhost"),
      [],
      new EncryptionSettings(),
    );
    const keys = await machine.exportRoomKeys(() => true);
    const ciphertext = await encryptAccountBackup(
      {
        handle: "fixture",
        control_url: "http://localhost:8080",
        registration_secret: "f".repeat(64),
        room_keys_json: keys,
      },
      password,
    );
    expect(new TextDecoder().decode(ciphertext)).not.toContain(
      "registration_secret",
    );
    expect(new TextDecoder().decode(ciphertext).split("\n")[1]).toMatch(/^-> scrypt \S+ 18$/);
    const decrypt = new Decrypter();
    decrypt.addPassphrase(password);
    const bundle = JSON.parse(await decrypt.decrypt(ciphertext, "text"));
    expect(bundle.format).toBe("zavliq-recovery-v1");
    expect(bundle.registration_secret).toBe("f".repeat(64));
    expect(
      JSON.parse(await decryptRoomKeyFile(bundle.room_keys, password)),
    ).toEqual(JSON.parse(keys));
    const wrong = new Decrypter();
    wrong.addPassphrase("different public fixture password");
    await expect(wrong.decrypt(ciphertext, "text")).rejects.toThrow();
    if (process.env.ZAVLIQ_WRITE_INTEROP_FIXTURE === "1")
      writeFileSync("fixtures/recovery-interoperability.age", ciphertext);
  } finally {
    machine.close();
  }
}, 60_000);
