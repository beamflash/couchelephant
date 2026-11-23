// Run through the emulator: firebase emulators:exec --only firestore "npm test"
const { test, before, after, beforeEach } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const {
  initializeTestEnvironment, assertSucceeds, assertFails,
} = require("@firebase/rules-unit-testing");
const {
  doc, setDoc, getDoc, getDocs, collection, updateDoc, deleteDoc, Timestamp, Bytes,
} = require("firebase/firestore");

const ID = "0123456789abcdef0123456789abcdef";
let env, db;

function report(over = {}) {
  const sealed = Bytes.fromUint8Array(new Uint8Array(100));
  return {
    reportId: ID, sealed, size: 100, created: Timestamp.now(),
    source: "web", appVersion: "1.2.3", keyId: "k1", installId: "i1", ...over,
  };
}

before(async () => {
  env = await initializeTestEnvironment({
    projectId: "ce-rules-test",
    firestore: {
      rules: fs.readFileSync(path.join(__dirname, "../../firestore.rules"), "utf8"),
      host: process.env.FIRESTORE_EMULATOR_HOST?.split(":")[0] || "127.0.0.1",
      port: Number((process.env.FIRESTORE_EMULATOR_HOST || ":8080").split(":")[1]),
    },
  });
  db = env.unauthenticatedContext().firestore();
});
after(async () => { await env.cleanup(); });
beforeEach(async () => { await env.clearFirestore(); });

const ref = (id = ID) => doc(db, "reports", id);
const seed = () => env.withSecurityRulesDisabled(
  (c) => setDoc(doc(c.firestore(), "reports", ID), report()));

test("a valid new report is accepted", async () => {
  await assertSucceeds(setDoc(ref(), report()));
});
test("read is denied", async () => {
  await seed();
  await assertFails(getDoc(ref()));
});
test("list is denied", async () => {
  await seed();
  await assertFails(getDocs(collection(db, "reports")));
});
test("update is denied", async () => {
  await seed();
  await assertFails(updateDoc(ref(), { source: "ios" }));
  await assertFails(setDoc(ref(), report()));
});
test("delete is denied", async () => {
  await seed();
  await assertFails(deleteDoc(ref()));
});
test("an extra field is denied", async () => {
  await assertFails(setDoc(ref(), report({ extra: "x" })));
});
test("a missing field is denied", async () => {
  const r = report(); delete r.installId;
  await assertFails(setDoc(ref(), r));
});
test("sealed as a string is denied", async () => {
  await assertFails(setDoc(ref(), report({ sealed: "abc" })));
});
test("900000 bytes is accepted, 900001 is denied", async () => {
  const big = (n) => report({ sealed: Bytes.fromUint8Array(new Uint8Array(n)), size: n });
  await assertSucceeds(setDoc(ref(), big(900000)));
  await assertFails(setDoc(ref("a".repeat(32)), big(900001)));
});
test("size that does not match sealed is denied", async () => {
  await assertFails(setDoc(ref(), report({ size: 99 })));
});
test("a bad source is denied", async () => {
  await assertFails(setDoc(ref(), report({ source: "android" })));
});
test("every allowed source is accepted", async () => {
  for (const [i, s] of ["web", "ios", "ipados", "tvos", "server"].entries()) {
    const id = String(i).repeat(32);
    await assertSucceeds(setDoc(ref(id), report({ reportId: id, source: s })));
  }
});
test("an id and reportId mismatch is denied", async () => {
  await assertFails(setDoc(ref(), report({ reportId: "f".repeat(32) })));
});
test("an id that is not 32 lowercase hex is denied", async () => {
  for (const id of ["short", "G".repeat(32), "A".repeat(32), "a".repeat(33)]) {
    await assertFails(setDoc(ref(id), report({ reportId: id })));
  }
});
test("a long string field is denied", async () => {
  await assertFails(setDoc(ref(), report({ appVersion: "x".repeat(65) })));
  await assertFails(setDoc(ref(), report({ keyId: "x".repeat(65) })));
  await assertFails(setDoc(ref(), report({ installId: "x".repeat(65) })));
});
const off = (days) => Timestamp.fromMillis(Date.now() + days * 86400000);
test("a created time far in the past or future is denied", async () => {
  await assertFails(setDoc(ref(), report({ created: off(-31) })));
  await assertFails(setDoc(ref(), report({ created: off(3) })));
});
test("a report queued for days is still accepted", async () => {
  await assertSucceeds(setDoc(ref(), report({ created: off(-3) })));
  await assertSucceeds(setDoc(ref("b".repeat(32)), report({ reportId: "b".repeat(32), created: off(-29) })));
});
test("created as a string is denied", async () => {
  await assertFails(setDoc(ref(), report({ created: "2026-11-21T00:00:00Z" })));
});
test("size as a string is denied", async () => {
  await assertFails(setDoc(ref(), report({ size: "100" })));
});
test("a create under another collection is denied", async () => {
  await assertFails(setDoc(doc(db, "other", ID), report()));
});
test("a create in a subcollection of reports is denied", async () => {
  await assertFails(setDoc(doc(db, "reports", ID, "notes", "n1"), report()));
});
