##########################################################################
# Info:
# Synchronous OCI Document Classification + Key-Value Extraction
# Classified document_type is passed into AnalyzeDocument
##########################################################################

import oci
import base64
import json

# --- Setup config ---
import os
CONFIG_PROFILE = "DEFAULT"
# Determine the directory of the current script
base_dir = os.path.dirname(os.path.abspath(__file__))
# Construct the path to the config file (assuming it's in ../config/config.txt relative to this script)
config_path = os.path.join(base_dir, "..", "config", "config.txt")
config = oci.config.from_file(config_path, CONFIG_PROFILE)

## config = oci.config.from_file("~/.oci/config", CONFIG_PROFILE)

COMPARTMENT_ID = "ocid1.tenancy.oc1..aaaaaaaa3oosoab4cce5nsseh5awuzuoemnhiqjt76qpznviscwwlbrmnqna"
# ---------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------
def load_and_encode_file(file_path: str) -> str:
    with open(file_path, "rb") as f:
        encoded_data = base64.b64encode(f.read()).decode("utf-8")
    return encoded_data


def classify_document(ai_doc_client, encoded_data: str) -> str:
    """First step → get document type using classification"""
    classification_feature = oci.ai_document.models.DocumentClassificationFeature()

    classification_details = oci.ai_document.models.AnalyzeDocumentDetails(
        compartment_id=COMPARTMENT_ID,
        features=[classification_feature], 
        document=oci.ai_document.models.InlineDocumentDetails(data=encoded_data)
    )

    print("\n[Info] Running Classification ...")
    clf_response = ai_doc_client.analyze_document(analyze_document_details=classification_details)

    result = oci.util.to_dict(clf_response.data)
    print(result)
    print(f"Classify HTTP Status: {clf_response.status}")

    # Extract the document type from result
    doc_type = None
    try:
        doc_type = result["detected_document_types"][0]["document_type"]
    except Exception:
        pass

    if not doc_type:
        print("[Warning] No classification detected. Defaulting to 'INVOICE'")
        doc_type = "INVOICE"

    print(f"[Info] Classified Document Type: {doc_type}")
    return doc_type


def extract_key_value_pairs(result: dict):
    if 'pages' in result and result['pages']:
        print("\n=== Key-Value Pairs Found ===")
        for page in result['pages']:
            if page.get('document_fields'):
                for field in page['document_fields']:
                    label = field.get('field_label', {})
                    if label is None:
                        label = {}
                    name = label.get('name', 'Unknown') if isinstance(label, dict) else 'Unknown'
                    
                    value_obj = field.get('field_value', {})
                    if value_obj is None:
                        value_obj = {}
                    val = value_obj.get('value', 'N/A') if isinstance(value_obj, dict) else 'N/A'
                    
                    print(f"{name}: {val}")


# ---------------------------------------------------------------
# Main Processing Logic
# ---------------------------------------------------------------
def process_document(file_path: str, encoded_data: str = None):
    
    # --- Encode File ---
    if not encoded_data:
        encoded_data = load_and_encode_file(file_path)

    ai_doc_client = oci.ai_document.AIServiceDocumentClient(config=config)

    # -----------------------------------------------------------
    # 🔹 Step 1: CLASSIFY DOCUMENT (Sync)
    # -----------------------------------------------------------
    detected_doc_type = classify_document(ai_doc_client, encoded_data)

    # -----------------------------------------------------------
    # 🔹 Step 2: TEXT + KEY VALUE EXTRACTION (Sync)
    # -----------------------------------------------------------
    text_feature = oci.ai_document.models.DocumentTextExtractionFeature()
    key_value_feature = oci.ai_document.models.DocumentKeyValueExtractionFeature()

    analyze_details = oci.ai_document.models.AnalyzeDocumentDetails(
        compartment_id=COMPARTMENT_ID,
        features=[text_feature, key_value_feature],
        document=oci.ai_document.models.InlineDocumentDetails(data=encoded_data),
        document_type=detected_doc_type  # ← dynamic doc type from classifier
    )

    print("\n[Info] Running Key-Value Extraction ...")
    kv_response = ai_doc_client.analyze_document(analyze_document_details=analyze_details)

    print(f"Extract HTTP Status: {kv_response.status}")

    result = oci.util.to_dict(kv_response.data)

    # Pretty print results
    extract_key_value_pairs(result)

    # Store JSON
    output_file = "classified_results_inline.json"
    with open(output_file, "w") as out:
        json.dump(result, out, indent=2, default=str)

    print(f"\n[Info] Results saved: {output_file}")
    print("\n=== Processing Complete ===")

    return {"document_type": detected_doc_type, "extraction_result": result}


# ---------------------------------------------------------------
# Run
# ---------------------------------------------------------------
#if __name__ == "__main__":
#    test_file = "/Users/srikarbala/Downloads/20240311_204946.jpg"  # Change this
#    process_document(test_file)
