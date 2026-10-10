"""Run: python3 tests.py   (uses a temporary database, no real M-Pesa calls)"""
import os, sys, tempfile, time, unittest

tmp = tempfile.mkdtemp()
os.environ.update(DATABASE=os.path.join(tmp, "t.db"), SECRET_KEY="test-secret", MPESA_CALLBACK_SECRET="cbsecret",
                  ADMIN_EMAIL="admin@swiftrun.test", ADMIN_PASSWORD="admin-pass-123",
                  MPESA_MODE="mock", MPESA_MOCK_DELAY="0.2", RATE_LIMIT="0", ALLOWED_ORIGINS="https://me.github.io")
sys.path.insert(0, os.path.dirname(__file__))
import app as srv

c = srv.app.test_client()
H = lambda t: {"Authorization": "Bearer " + t}
ORDER = dict(name="Alice Wanjiku", phone="0712 345 678", email="alice@email.com", desc="Deliver a parcel",
             pickup="CBD, Moi Avenue", dropoff="Westlands, Sarit", service="parcel", goodsValue=2000, payMethod="mpesa")


class T(unittest.TestCase):
    def admin(self):
        r = c.post("/api/auth/login", json={"email": "admin@swiftrun.test", "password": "admin-pass-123"})
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json["user"]["role"], "admin")
        return r.json["token"]

    def test_01_auth(self):
        self.assertEqual(c.post("/api/auth/register", json={"first": "A", "last": "B", "email": "bad", "password": "longenough"}).status_code, 400)
        self.assertEqual(c.post("/api/auth/register", json={"first": "A", "last": "B", "email": "a@b.co", "password": "short"}).status_code, 400)
        r = c.post("/api/auth/register", json={"first": "Alice", "last": "W", "email": "Alice@Email.com", "password": "password123"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(c.post("/api/auth/register", json={"first": "A", "last": "W", "email": "alice@email.com", "password": "password123"}).status_code, 409)
        self.assertEqual(c.post("/api/auth/login", json={"email": "alice@email.com", "password": "wrong"}).status_code, 401)
        self.assertEqual(c.get("/api/auth/me", headers=H(r.json["token"])).json["user"]["email"], "alice@email.com")
        self.assertEqual(c.get("/api/auth/me", headers=H("garbage")).status_code, 401)

    def test_02_quote_and_validation(self):
        q = c.post("/api/quote", json={"service": "parcel", "goodsValue": 2000}).json
        self.assertEqual((q["distFee"], q["goodsFee"], q["total"]), (200, 100, 300))
        q = c.post("/api/quote", json={"service": "parcel", "goodsValue": 0, "distanceKm": 10}).json
        self.assertEqual(q["distFee"], 500)
        self.assertEqual(c.post("/api/quote", json={"service": "nope"}).status_code, 400)
        self.assertEqual(c.post("/api/orders", json={**ORDER, "phone": "123"}).status_code, 400)
        self.assertEqual(c.post("/api/orders", json={**ORDER, "goodsValue": -5}).status_code, 400)
        self.assertEqual(c.post("/api/orders", json={**ORDER, "payMethod": "card"}).status_code, 400)
        self.assertEqual(c.post("/api/orders", data="not json", content_type="text/plain").status_code, 400)

    def test_03_full_mpesa_flow(self):
        r = c.post("/api/orders", json=ORDER)
        self.assertEqual(r.status_code, 201)
        o = r.json["order"]
        self.assertTrue(o["id"].startswith("SR-10")); self.assertEqual(o["total"], 300)
        self.assertEqual((o["status"], o["payStatus"], o["customer"]["phone"]), ("pending", "unpaid", "+254712345678"))
        self.assertEqual((o["category"], o["service"]), ("Delivery & Courier", "Parcel delivery"))
        self.assertEqual(len(o["timeline"]), 5)
        p = c.post(f"/api/orders/{o['id']}/pay/mpesa", json={})
        self.assertEqual(p.status_code, 202)
        time.sleep(0.8)
        st = c.get(f"/api/orders/{o['id']}/payment").json
        self.assertEqual((st["state"], st["payStatus"], st["status"]), ("completed", "paid", "confirmed"))
        self.assertTrue(st["mpesaRef"].startswith("MOCK"))
        self.assertEqual(c.post(f"/api/orders/{o['id']}/pay/mpesa", json={}).status_code, 409)  # already paid
        got = c.get(f"/api/orders/track?q={o['id']}").json["orders"][0]
        self.assertIsNotNone(got["rider"]); self.assertTrue(got["timeline"][2]["done"])
        self.assertEqual(c.get("/api/orders/track?q=0712345678").json["orders"][0]["id"], o["id"])
        self.assertEqual(c.get("/api/orders/track?q=alice@email.com").json["orders"][0]["id"], o["id"])
        self.assertEqual(c.get("/api/orders/track?q=0799000000").json["orders"], [])
        self.assertEqual(c.get("/api/orders/track?q=abc").status_code, 400)

    def test_04_admin_flow_and_cash(self):
        a = self.admin()
        o = c.post("/api/orders", json={**ORDER, "payMethod": "cash", "service": "gas"}).json["order"]
        self.assertEqual((o["status"], o["payStatus"]), ("confirmed", "cash_on_delivery"))
        self.assertEqual(c.patch(f"/api/admin/orders/{o['id']}", json={"action": "complete"}).status_code, 401)
        bob = c.post("/api/auth/register", json={"first": "Bob", "last": "K", "email": "bob@x.co", "password": "password123"}).json["token"]
        self.assertEqual(c.patch(f"/api/admin/orders/{o['id']}", json={"action": "complete"}, headers=H(bob)).status_code, 403)
        self.assertEqual(c.patch(f"/api/admin/orders/{o['id']}", json={"action": "complete"}, headers=H(a)).status_code, 409)
        d = c.patch(f"/api/admin/orders/{o['id']}", json={"action": "dispatch", "riderId": "R002"}, headers=H(a)).json["order"]
        self.assertEqual((d["status"], d["rider"]["id"]), ("in-transit", "R002"))
        self.assertEqual(c.patch(f"/api/admin/orders/{o['id']}", json={"action": "dispatch", "riderId": "R004"}, headers=H(a)).status_code, 409)
        before = [r for r in c.get("/api/admin/riders", headers=H(a)).json["riders"] if r["id"] == "R002"][0]
        d = c.patch(f"/api/admin/orders/{o['id']}", json={"action": "complete"}, headers=H(a)).json["order"]
        self.assertEqual((d["status"], d["payStatus"]), ("completed", "paid"))
        after = [r for r in c.get("/api/admin/riders", headers=H(a)).json["riders"] if r["id"] == "R002"][0]
        self.assertEqual(after["trips"], before["trips"] + 1)
        pays = c.get("/api/admin/payments", headers=H(a)).json["payments"]
        self.assertTrue(any(p["orderId"] == o["id"] and p["method"] == "Cash" for p in pays))
        self.assertGreaterEqual(len(c.get("/api/admin/orders", headers=H(a)).json["orders"]), 2)

    def test_05_cancel_rules(self):
        o = c.post("/api/orders", json=ORDER).json["order"]
        self.assertEqual(c.post(f"/api/orders/{o['id']}/cancel", json={"phone": "0700000000"}).status_code, 404)
        r = c.post(f"/api/orders/{o['id']}/cancel", json={"phone": "+254712345678"})
        self.assertEqual(r.json["order"]["status"], "cancelled")
        self.assertEqual(c.post(f"/api/orders/{o['id']}/cancel", json={"phone": "0712345678"}).status_code, 409)
        self.assertEqual(c.post(f"/api/orders/{o['id']}/pay/mpesa", json={}).status_code, 409)

    def test_06_user_orders_and_privacy(self):
        t = c.post("/api/auth/login", json={"email": "alice@email.com", "password": "password123"}).json["token"]
        mine = c.post("/api/orders", json=ORDER, headers=H(t)).json["order"]
        lst = c.get("/api/orders", headers=H(t)).json["orders"]
        self.assertTrue(any(x["id"] == mine["id"] for x in lst))
        bob = c.post("/api/auth/login", json={"email": "bob@x.co", "password": "password123"}).json["token"]
        self.assertEqual(c.get("/api/orders", headers=H(bob)).json["orders"], [])
        self.assertEqual(c.get(f"/api/orders/{mine['id']}", headers=H(bob)).status_code, 404)
        self.assertEqual(c.get(f"/api/orders/{mine['id']}", headers=H(t)).status_code, 200)

    def test_07_daraja_callback(self):
        o = c.post("/api/orders", json=ORDER).json["order"]
        db = srv.open_db()
        db.execute("INSERT INTO payments(order_num,method,phone,amount,status,checkout_id,created_at) VALUES(?,?,?,?,?,?,?)",
                   (int(o["id"][3:]), "M-Pesa", "+254712345678", 300, "pending", "ws_CO_1", srv.now_iso())); db.commit(); db.close()
        cb = lambda amt, code=0: {"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1", "ResultCode": code, "ResultDesc": "x",
              "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": amt}, {"Name": "MpesaReceiptNumber", "Value": "QWE123ABC"}]}}}}
        self.assertEqual(c.post("/api/mpesa/callback/wrong", json=cb(300)).status_code, 404)
        c.post("/api/mpesa/callback/cbsecret", json=cb(1))     # wrong amount must not confirm
        self.assertEqual(c.get(f"/api/orders/{o['id']}/payment").json["payStatus"], "unpaid")
        db = srv.open_db(); db.execute("UPDATE payments SET status='pending' WHERE checkout_id='ws_CO_1'"); db.commit(); db.close()
        c.post("/api/mpesa/callback/cbsecret", json=cb(300))
        s = c.get(f"/api/orders/{o['id']}/payment").json
        self.assertEqual((s["payStatus"], s["mpesaRef"]), ("paid", "QWE123ABC"))
        c.post("/api/mpesa/callback/cbsecret", json=cb(300))   # replay is harmless
        self.assertEqual(c.get(f"/api/orders/{o['id']}/payment").json["state"], "completed")

    def test_08_riders_crud_cors_static(self):
        a = self.admin()
        r = c.post("/api/admin/riders", json={"name": "Test Rider", "phone": "0722000111", "vehicle": "Motorbike", "plate": "KAA 1A"}, headers=H(a))
        self.assertEqual(r.status_code, 201); rid = r.json["rider"]["id"]
        self.assertEqual(c.patch(f"/api/admin/riders/{rid}", json={"status": "offline"}, headers=H(a)).json["rider"]["status"], "offline")
        self.assertEqual(c.patch(f"/api/admin/riders/{rid}", json={"status": "x"}, headers=H(a)).status_code, 400)
        self.assertEqual(c.delete(f"/api/admin/riders/{rid}", headers=H(a)).status_code, 200)
        h = c.get("/api/health", headers={"Origin": "https://me.github.io"}).headers
        self.assertEqual(h["Access-Control-Allow-Origin"], "https://me.github.io")
        self.assertNotIn("Access-Control-Allow-Origin", c.get("/api/health", headers={"Origin": "https://evil.com"}).headers)
        self.assertEqual(c.get("/api/health", headers={"Origin": "http://127.0.0.1:5500"}).headers["Access-Control-Allow-Origin"], "http://127.0.0.1:5500")
        self.assertNotIn("Access-Control-Allow-Origin", c.get("/api/health", headers={"Origin": "http://localhost.evil.com"}).headers)
        for bad in ("/.env", "/backend/app.py", "/swiftrun.db", "/app.py"):
            self.assertEqual(c.get(bad).status_code, 404, bad)
        self.assertEqual(len(c.get("/api/services").json["services"]), 22)

if __name__ == "__main__":
    unittest.main(verbosity=2)
