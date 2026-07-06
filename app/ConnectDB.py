import toml
import oracledb
import pandas as pd
import logging
from pathlib import Path
from typing import Optional, Dict
import sys
import json

# ---------- Configure Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("oracle_fetcher.log"),
        logging.StreamHandler()
    ]
)

# ---------- Load Config ----------
def load_config(config_path: str = "config.toml") -> Optional[Dict[str, str]]:
    try:
        config = toml.load(config_path)
        db = config["oracle_atp"]
        return {
            "user": str(db["user"]),
            "password": str(db["password"]),
            "host": db["host"],
            "port": db["port"],
            "service_name": db["service_name"],
            "wallet_path": str(db.get("wallet_location")),
            "config_path": str(db.get("config_dir")),
            "wallet_pass": str(db["wallet_pass"])
        }
    except FileNotFoundError:
        logging.error("Configuration file not found.")
    except KeyError as e:
        logging.error(f"Missing expected key in config: {e}")
    except Exception as e:
        logging.exception("Unexpected error while loading config.")
    return None

# ---------- Build DSN ----------
def build_dsn(db: Dict[str, str]) -> str:
    return (
        "(description=(retry_count=20)(retry_delay=3)"
        f"(address=(protocol=tcps)(port={db['port']})(host={db['host']}))"
        f"(connect_data=(service_name={db['service_name']}))"
        "(security=(ssl_server_dn_match=yes)))"
    )

# ---------- Execute DB Query ----------
def execute_db(query_param: str, db: Dict[str, str]) -> Optional[pd.DataFrame]:
    try:
        dsn = build_dsn(db)
        print(dsn)
        wallet_loc = db["wallet_path"]
        config_loc = db["config_path"]

        conn = oracledb.connect(
            #config_dir=str(config_loc),
            user=db["user"],
            password=db["password"],
            dsn=dsn,
            wallet_location=str(wallet_loc),
            wallet_password=db["wallet_pass"]
        )
        cursor = conn.cursor()

        base_query = 'SELECT INTEGRATIONNAME, INTEGRATIONINSTANCEID, SOURCEPAYLOAD FROM XXNTTD_SIGMOID_EXCEPTIONS'
        if query_param.lower() != 'all':
            base_query += f" WHERE INTEGRATIONINSTANCEID = :1"
            logging.info(f"Executing filtered query for module: {query_param}")
            cursor.execute(base_query, [query_param])
        else:
            logging.info("Executing full query.")
            cursor.execute(base_query)

        rows = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        
        #df = pd.DataFrame(rows, columns=columns)

        '''test_info = df.filter(items=[
            'S_NO', 'OFFERING', 'MODULE', 'L2_PROCESS_NAME', 'L3_PROCESS_NAME',
            'BUSINESS_REQUIREMENT_NUMBER', 'TEST_SCENARIO_NUMBER', 'VERSION', 'TEST_SCRIPT'
        ])'''

        #logging.info(f"Fetched {len(test_info)} rows from the database.")
        #clob_payload = df["SOURCEPAYLOAD"]
        result = [{desc[0]: lob_to_serializable(val) for desc, val in zip(cursor.description, row)}
                for row in rows
            ]

        #json_str = json.dumps(result)
        #print(json_str)
        return result

    except oracledb.DatabaseError as db_err:
        logging.error(f"Database error: {db_err}")
    except Exception:
        logging.exception("Unexpected error during DB execution.")
    finally:
        try:
            cursor.close()
            conn.close()
        except:
            pass
    return None

# ---- Conver the CLOB columns o string ----

def lob_to_serializable(value):
    if hasattr(value, "read"):  # LOB object
        data = value.read()
        return data.decode("utf-8") if isinstance(data, bytes) else data
    return value


# ---------- Main ----------
def dbmain(query_param: str) -> Optional[dict]:
    logging.info(f"Starting DB query for module: {query_param}")
    db_config = load_config()
    if not db_config:
        logging.error("Failed to load database config.")
        return None
    else:
        result_df = execute_db(query_param, db_config)
    if result_df is not None:
        logging.info("DB fetch successful.")
    else:
        logging.warning("No results returned or an error occurred.")
    return result_df

# ---------- Entry Point ----------
if __name__ == "__main__":
    query_param = sys.argv[1] if len(sys.argv) > 1 else "All"
    df = dbmain(query_param)
    if df is not None:
        logging.info("\n" + df)

  