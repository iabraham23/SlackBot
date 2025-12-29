"""
Reads all json files from doc_info directory and creates in same file:
- claude_summary: A concise summary of the article content
- keywords: Relevant keywords for the document

Uses Anthropic's Batch API
"""

import os
import json
import logging
import time
from pathlib import Path
from typing import List, Dict, Tuple
import anthropic
from dotenv import load_dotenv
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Claude client
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Configuration
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
DOC_INFO_DIR = Path("doc_info")
BATCH_POLL_INTERVAL = 10  # seconds between polling


def read_doc_info(filepath: Path) -> dict | None:
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading {filepath}: {e}")
        return None


def update_doc_info(filepath: Path, doc: dict) -> bool:
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=2)
        logger.info(f"Updated: {filepath.name}")
        return True
    except Exception as e:
        logger.error(f"Error writing {filepath}: {e}")
        return False


def build_prompt(title: str, article: str) -> str:
    return f"""Analyze this help center article and provide a JSON response with exactly this structure:
{{
    "claude_summary": "A concise summary of the article (max 30 words)",
    "keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]
}}

Provide 5-10 relevant keywords that would help users find this article.
Respond ONLY with valid JSON, no other text.

Document title: {title}

Article content:
{article}"""


def parse_response_text(response_text: str) -> dict | None:
    
    text = response_text.strip()
    # Handle markdown code blocks
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON: {e}")
        return None


def collect_documents_to_process(doc_info_dir: Path, force: bool = False) -> List[Tuple[Path, dict]]:
   
    to_process = []
    if not doc_info_dir.exists():
        logger.error(f"Directory not found: {doc_info_dir}")
        return to_process
    
    for filepath in doc_info_dir.glob("*.json"):
        doc = read_doc_info(filepath)
        if doc is None:
            continue
        
        # Skip if already indexed (unless force)
        if not force and "claude_summary" in doc and "keywords" in doc:
            logger.info(f"Skipping (already indexed): {filepath.name}")
            continue
        
        # Skip if no article content
        if not doc.get("article"):
            logger.warning(f"No article content in {filepath.name}, skipping")
            continue
        
        to_process.append((filepath, doc))
    
    return to_process


def create_batch_requests(documents: List[Tuple[Path, dict]]) -> Tuple[List[dict], Dict[str, Path]]:

    requests = []
    id_to_filepath = {}
    
    for i, (filepath, doc) in enumerate(documents):
        title = doc.get("title", filepath.stem)
        article = doc.get("article", "")
        custom_id = f"doc_{i}"
        
        id_to_filepath[custom_id] = filepath
        
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": CLAUDE_MODEL,
                "max_tokens": 300,
                "messages": [
                    {
                        "role": "user",
                        "content": build_prompt(title, article)
                    }
                ]
            }
        })
    
    return requests, id_to_filepath


def submit_batch(requests: List[dict]) -> str | None:
    
    logger.info(f"Submitting batch with {len(requests)} requests")
    
    try:
        batch = client.messages.batches.create(requests=requests)
        logger.info(f"Batch created with ID: {batch.id}")
        return batch.id
    except Exception as e:
        logger.error(f"Error creating batch: {e}")
        return None


def poll_batch_status(batch_id: str) -> str:
    
    logger.info(f"Polling batch {batch_id} for completion...")
    
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        status = batch.processing_status
        
        logger.info(f"Batch status: {status}")
        
        if status == "ended":
            logger.info(f"Batch complete. Succeeded: {batch.request_counts.succeeded}, "
                       f"Failed: {batch.request_counts.errored}")
            return status
        elif status in ("failed", "canceled", "expired"):
            logger.error(f"Batch ended with status: {status}")
            return status
        
        time.sleep(BATCH_POLL_INTERVAL)


def process_batch_results(batch_id: str) -> Dict[str, dict]:

    results = {}
    
    try:
        for result in client.messages.batches.results(batch_id):
            custom_id = result.custom_id
            
            if result.result.type == "succeeded":
                response_text = result.result.message.content[0].text
                parsed = parse_response_text(response_text)
                
                if parsed:
                    results[custom_id] = parsed
                else:
                    logger.error(f"Failed to parse result for {custom_id}")
            else:
                logger.error(f"Request failed for {custom_id}: {result.result.type}")
    
    except Exception as e:
        logger.error(f"Error retrieving batch results: {e}")
    
    return results


def update_documents_with_results(
    documents: List[Tuple[Path, dict]], 
    results: Dict[str, dict],
    id_to_filepath: Dict[str, Path]
) -> dict:
    stats = {"processed": 0, "failed": 0}
    
    # Create filepath to doc mapping for lookup
    filepath_to_doc = {filepath: doc for filepath, doc in documents}
    
    for custom_id, summary_data in results.items():
        filepath = id_to_filepath.get(custom_id)
        
        if filepath is None:
            logger.warning(f"No filepath found for {custom_id}")
            stats["failed"] += 1
            continue
        
        doc = filepath_to_doc.get(filepath)
        if doc is None:
            logger.warning(f"No document found for {filepath}")
            stats["failed"] += 1
            continue
        
        doc["claude_summary"] = summary_data.get("claude_summary", "")
        doc["keywords"] = summary_data.get("keywords", [])
        
        if update_doc_info(filepath, doc):
            stats["processed"] += 1
        else:
            stats["failed"] += 1
    
    # Count missing docs
    missing = len(documents) - len(results)
    if missing > 0:
        logger.warning(f"{missing} documents did not receive results")
        stats["failed"] += missing
    
    return stats


def process_all_documents_batch(doc_info_dir: Path = DOC_INFO_DIR, force: bool = False) -> dict:
 
    documents = collect_documents_to_process(doc_info_dir, force)
    
    if not documents:
        logger.info("No documents to process")
        return {"processed": 0, "skipped": 0, "failed": 0}
    
    logger.info(f"Found {len(documents)} documents to process")
    
    requests, id_to_filepath = create_batch_requests(documents)
    batch_id = submit_batch(requests)
    
    if batch_id is None:
        return {"processed": 0, "skipped": 0, "failed": len(documents)}
    
    # Try getting completion
    status = poll_batch_status(batch_id)
    
    if status != "ended":
        return {"processed": 0, "skipped": 0, "failed": len(documents)}
    
    results = process_batch_results(batch_id)
    stats = update_documents_with_results(documents, results, id_to_filepath)
    
    json_files = list(doc_info_dir.glob("*.json"))
    stats["skipped"] = len(json_files) - len(documents)
    
    logger.info(f"Processing complete: {stats}")
    return stats


def print_summary(doc_info_dir: Path = DOC_INFO_DIR):

    json_files = list(doc_info_dir.glob("*.json"))
    
    print(f"\n{'='*60}")
    print(f"Document Info Summary ({len(json_files)} documents)")
    print(f"{'='*60}")
    
    for filepath in json_files:
        doc = read_doc_info(filepath)
        if doc:
            print(f"\n {doc.get('title', 'Untitled')}")
            print(f"   Category: {doc.get('category', 'N/A')}")
            if "claude_summary" in doc:
                print(f"   Summary: {doc['claude_summary']}")
            if "keywords" in doc:
                print(f"   Keywords: {', '.join(doc['keywords'])}")
            else:
                print("  Not yet indexed")


if __name__ == "__main__":
    process_all_documents_batch()
    print_summary()
