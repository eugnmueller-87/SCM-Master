-- ============================================================
-- SCM order delivery tracking — seed data
-- PostgreSQL / PostgREST-ready. Matches the control-tower view.
-- Source of truth = shipment_events; shipments holds the
-- denormalized "current position" snapshot for fast reads.
-- ============================================================

DROP VIEW  IF EXISTS v_order_tracking CASCADE;
DROP TABLE IF EXISTS shipment_events  CASCADE;
DROP TABLE IF EXISTS shipments        CASCADE;
DROP TABLE IF EXISTS po_lines         CASCADE;
DROP TABLE IF EXISTS purchase_orders  CASCADE;
DROP TABLE IF EXISTS items            CASCADE;
DROP TABLE IF EXISTS suppliers        CASCADE;

-- ---------- master data ----------
CREATE TABLE suppliers (
    supplier_id   text PRIMARY KEY,
    name          text NOT NULL,
    country       char(2) NOT NULL,
    tier          smallint CHECK (tier BETWEEN 1 AND 3),
    payment_terms text DEFAULT 'NET30',
    currency      char(3) DEFAULT 'EUR'
);

CREATE TABLE items (
    item_id            text PRIMARY KEY,
    description        text NOT NULL,
    uom                text NOT NULL,
    unit_cost          numeric(12,4) NOT NULL,
    currency           char(3) DEFAULT 'EUR',
    preferred_supplier text REFERENCES suppliers(supplier_id)
);

-- ---------- transactional ----------
CREATE TABLE purchase_orders (
    po_id             text PRIMARY KEY,
    supplier_id       text NOT NULL REFERENCES suppliers(supplier_id),
    order_date        date NOT NULL,
    expected_delivery date NOT NULL,
    currency          char(3) DEFAULT 'EUR',
    total_value       numeric(14,2) NOT NULL,
    status            text NOT NULL DEFAULT 'open'
                      CHECK (status IN ('draft','open','partial','closed'))
);

CREATE TABLE po_lines (
    po_line_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    po_id       text NOT NULL REFERENCES purchase_orders(po_id),
    item_id     text NOT NULL REFERENCES items(item_id),
    qty         numeric(14,2) NOT NULL,
    unit_price  numeric(12,4) NOT NULL
);

CREATE TABLE shipments (
    shipment_id       text PRIMARY KEY,
    po_id             text NOT NULL REFERENCES purchase_orders(po_id),
    mode              text NOT NULL CHECK (mode IN ('road','ocean','air','rail')),
    carrier           text,
    -- denormalized current snapshot (latest event rolled up)
    current_status    text NOT NULL,
    progress_idx      smallint NOT NULL CHECK (progress_idx BETWEEN 0 AND 5),
    current_location  text,
    current_lat       double precision,
    current_lng       double precision,
    last_event_at     timestamptz,
    -- two ETAs: the frozen promise + the live estimate
    eta_original      date NOT NULL,
    eta_current       date NOT NULL,
    delay_days        integer GENERATED ALWAYS AS (eta_current - eta_original) STORED,
    exception_flag    boolean NOT NULL DEFAULT false,
    exception_reason  text
);

CREATE TABLE shipment_events (
    event_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shipment_id    text NOT NULL REFERENCES shipments(shipment_id),
    seq            smallint NOT NULL,
    status         text NOT NULL
                   CHECK (status IN ('placed','confirmed','packed','departed_origin',
                                     'in_transit','arrived_hub','customs',
                                     'out_for_delivery','delivered','exception')),
    location_name  text NOT NULL,
    lat            double precision,
    lng            double precision,
    event_ts       timestamptz NOT NULL,
    notes          text,
    UNIQUE (shipment_id, seq)
);

-- ============================================================
-- SEED
-- ============================================================

INSERT INTO suppliers (supplier_id,name,country,tier,payment_terms) VALUES
 ('S001','Shenzhen Optics Co.','CN',2,'NET45'),
 ('S002','Pan-Asia Distribution','TW',1,'NET60'),
 ('S003','Bosch Rexroth','DE',1,'NET30'),
 ('S004','Murata Mfg.','JP',1,'NET30'),
 ('S005','Würth Group','DE',1,'NET14'),
 ('S006','Berliner Verpackung','DE',3,'NET30');

INSERT INTO items (item_id,description,uom,unit_cost,preferred_supplier) VALUES
 ('ITM-CAM-01','Camera module 5MP','each',16.8400,'S001'),
 ('ITM-MCU-01','MCU wafer lot','lot',7000.0000,'S002'),
 ('ITM-HYDV-01','Hydraulic valve, 4-way','each',262.5000,'S003'),
 ('ITM-CAP-01','Capacitor 0402 X7R','each',0.0945,'S004'),
 ('ITM-FAST-01','Fastener assortment kit','kit',6420.0000,'S005'),
 ('ITM-PKG-01','Pallet packaging set','lot',2150.0000,'S006');

INSERT INTO purchase_orders (po_id,supplier_id,order_date,expected_delivery,total_value,status) VALUES
 ('PO-10288','S001','2026-04-12','2026-04-30',84200.00,'open'),
 ('PO-10310','S002','2026-04-25','2026-05-19',210000.00,'open'),
 ('PO-10293','S003','2026-04-28','2026-05-05',31500.00,'open'),
 ('PO-10301','S004','2026-04-26','2026-05-04',18900.00,'open'),
 ('PO-10275','S005','2026-04-28','2026-05-03',6420.00,'closed'),
 ('PO-10312','S006','2026-05-03','2026-05-12',2150.00,'open');

INSERT INTO po_lines (po_id,item_id,qty,unit_price) VALUES
 ('PO-10288','ITM-CAM-01',5000,16.8400),
 ('PO-10310','ITM-MCU-01',30,7000.0000),
 ('PO-10293','ITM-HYDV-01',120,262.5000),
 ('PO-10301','ITM-CAP-01',200000,0.0945),
 ('PO-10275','ITM-FAST-01',1,6420.0000),
 ('PO-10312','ITM-PKG-01',1,2150.0000);

INSERT INTO shipments
 (shipment_id,po_id,mode,carrier,current_status,progress_idx,current_location,
  current_lat,current_lng,last_event_at,eta_original,eta_current,exception_flag,exception_reason) VALUES
 ('SHP-0288','PO-10288','ocean','Maersk','customs',3,'Hamburg customs, DE',
   53.5500,9.9900,'2026-05-02 09:20+02','2026-04-30','2026-05-04',true,'Held — HS code documentation query'),
 ('SHP-0310','PO-10310','ocean','Evergreen','departed_origin',1,'Kaohsiung Port, TW',
   22.6163,120.2818,'2026-05-13 11:05+08','2026-05-19','2026-05-21',false,NULL),
 ('SHP-0293','PO-10293','road','DB Schenker','in_transit',2,'Frankfurt hub, DE',
   50.1109,8.6821,'2026-05-03 14:40+02','2026-05-05','2026-05-05',false,NULL),
 ('SHP-0301','PO-10301','air','Lufthansa Cargo','out_for_delivery',4,'Munich, DE',
   48.1374,11.5755,'2026-05-04 07:55+02','2026-05-04','2026-05-04',false,NULL),
 ('SHP-0275','PO-10275','road','Würth Logistik','delivered',5,'Berlin DC, DE',
   52.5200,13.4050,'2026-05-03 10:12+02','2026-05-03','2026-05-03',false,NULL),
 ('SHP-0312','PO-10312','road','local','placed',0,'Berlin, DE',
   52.5200,13.4050,'2026-05-03 16:00+02','2026-05-12','2026-05-12',false,NULL);

INSERT INTO shipment_events (shipment_id,seq,status,location_name,lat,lng,event_ts,notes) VALUES
 -- SHP-0288 : ocean, customs hold (the exception)
 ('SHP-0288',1,'placed','Shenzhen, CN',22.5429,114.0596,'2026-04-12 10:00+08','Order confirmed by supplier'),
 ('SHP-0288',2,'packed','Shenzhen, CN',22.5429,114.0596,'2026-04-18 17:30+08','Packed, awaiting vessel'),
 ('SHP-0288',3,'departed_origin','Yantian Port, CN',22.5700,114.2700,'2026-04-22 08:15+08','Loaded on MV Hanjin (ETD)'),
 ('SHP-0288',4,'arrived_hub','Port of Hamburg, DE',53.5400,9.9700,'2026-04-28 06:40+02','Container discharged'),
 ('SHP-0288',5,'customs','Hamburg customs, DE',53.5500,9.9900,'2026-05-02 09:20+02','Held — HS code documentation query'),
 -- SHP-0310 : ocean, at risk at origin port
 ('SHP-0310',1,'placed','Hsinchu, TW',24.8047,120.9714,'2026-04-25 09:00+08','Order confirmed'),
 ('SHP-0310',2,'packed','Hsinchu, TW',24.8047,120.9714,'2026-05-09 15:20+08','Packed & sealed'),
 ('SHP-0310',3,'departed_origin','Kaohsiung Port, TW',22.6163,120.2818,'2026-05-13 11:05+08','At port — vessel congestion, ETD slipping'),
 -- SHP-0293 : road, in transit, on time
 ('SHP-0293',1,'placed','Lohr am Main, DE',49.9897,9.5779,'2026-04-28 11:00+02','Order confirmed'),
 ('SHP-0293',2,'packed','Lohr am Main, DE',49.9897,9.5779,'2026-04-30 13:10+02','Picked & packed'),
 ('SHP-0293',3,'departed_origin','Würzburg, DE',49.7913,9.9534,'2026-05-02 08:30+02','Departed origin'),
 ('SHP-0293',4,'in_transit','Frankfurt hub, DE',50.1109,8.6821,'2026-05-03 14:40+02','In transit — line haul'),
 -- SHP-0301 : air, out for delivery, on time
 ('SHP-0301',1,'placed','Kyoto, JP',35.0116,135.7681,'2026-04-26 10:30+09','Order confirmed'),
 ('SHP-0301',2,'departed_origin','Kansai Airport, JP',34.4347,135.2441,'2026-04-29 22:15+09','Air freight departed'),
 ('SHP-0301',3,'arrived_hub','Frankfurt FRA, DE',50.0379,8.5622,'2026-05-01 05:50+02','Customs cleared at FRA'),
 ('SHP-0301',4,'out_for_delivery','Munich, DE',48.1374,11.5755,'2026-05-04 07:55+02','Out for delivery'),
 -- SHP-0275 : road, delivered
 ('SHP-0275',1,'placed','Künzelsau, DE',49.2817,9.6890,'2026-04-28 09:45+02','Order confirmed'),
 ('SHP-0275',2,'departed_origin','Künzelsau, DE',49.2817,9.6890,'2026-04-30 16:00+02','Dispatched'),
 ('SHP-0275',3,'delivered','Berlin DC, DE',52.5200,13.4050,'2026-05-03 10:12+02','Delivered — signed M. Krause'),
 -- SHP-0312 : just placed
 ('SHP-0312',1,'placed','Berlin, DE',52.5200,13.4050,'2026-05-03 16:00+02','PO issued & confirmed');

-- ============================================================
-- Read model for the control-tower / "where is it now" view.
-- Derives the status label (On time / At risk / Delayed) so the
-- UI never stores it. PostgREST exposes this as /v_order_tracking.
-- ============================================================
CREATE VIEW v_order_tracking AS
SELECT
    po.po_id,
    s.name              AS supplier,
    s.country,
    sh.shipment_id,
    sh.mode,
    sh.current_status,
    sh.progress_idx,
    sh.current_location,
    sh.current_lat,
    sh.current_lng,
    sh.last_event_at,
    sh.eta_original,
    sh.eta_current,
    sh.delay_days,
    sh.exception_flag,
    po.total_value,
    po.currency,
    CASE
        WHEN sh.current_status = 'delivered'        THEN 'Delivered'
        WHEN sh.exception_flag                      THEN 'Delayed'
        WHEN sh.delay_days > 0                      THEN 'At risk'
        ELSE 'On time'
    END                 AS status_label
FROM purchase_orders po
JOIN suppliers s   ON s.supplier_id = po.supplier_id
JOIN shipments sh  ON sh.po_id      = po.po_id;
