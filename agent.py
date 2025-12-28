import os
import json
import logging
from pathlib import Path
import anthropic
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


_client = None
def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


class DocumentIndex:
    def __init__(self):
        self.documents: dict[str, dict] = {}  # id -> full document
        self.loaded = False
        self._summaries_cache: str = ""  # Cache formatted summaries
    
    def load_from_directory(self, directory: str):

        dir_path = Path(directory)
        if not dir_path.exists():
            logger.warning(f"Directory not found: {directory}")
            return
        
        for filepath in dir_path.glob("*.json"):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    doc = json.load(f)
                
                # Use filename as ID
                doc_id = filepath.stem
                doc['id'] = doc_id
                doc['filepath'] = str(filepath)
                
                self.documents[doc_id] = doc
                logger.debug(f"Loaded: {doc.get('title', doc_id)}")
                
            except Exception as e:
                logger.error(f"Error loading {filepath}: {e}")
        
        self.loaded = True
        self._summaries_cache = ""  # Reset cache
        logger.info(f"Loaded {len(self.documents)} documents from {directory}")
    
    def get_summaries_for_selection(self) -> str:

        if self._summaries_cache:
            return self._summaries_cache
            
        lines = []
        for i, (doc_id, doc) in enumerate(self.documents.items(), 1):
            title = doc.get('title', 'Untitled')
            summary = doc.get('claude_summary', doc.get('short_description', ''))
            keywords = doc.get('keywords', [])
            category = doc.get('category', '')
            
            entry = f"[{i}] {doc_id}\n"
            entry += f"    Title: {title}\n"
            entry += f"    Category: {category}\n"
            entry += f"    Summary: {summary}\n"
            if keywords:
                entry += f"    Keywords: {', '.join(keywords)}\n"
            
            lines.append(entry)
        
        self._summaries_cache = "\n".join(lines)
        return self._summaries_cache
    
    def estimate_summary_tokens(self) -> int:
        summaries = self.get_summaries_for_selection()
        return len(summaries) // 4
    
    def get_documents_by_ids(self, doc_ids: list[str]) -> list[dict]:
        return [self.documents[d_id] for d_id in doc_ids if d_id in self.documents]


# Global document index
_index = DocumentIndex()


def load_documents(directory: str):
    _index.load_from_directory(directory)


def select_relevant_docs(query: str, max_docs: int = 3) -> list[str]:

    if not _index.loaded or len(_index.documents) == 0:
        logger.warning("No documents loaded. Call load_documents() first.")
        return []
    
    client = _get_client()
    
    # Build the prompt for Haiku
    summaries = _index.get_summaries_for_selection()
    
    # System message as array of content blocks for prompt caching
    # Static content (instructions + summaries) is cached
    system_blocks = [
        {
            "type": "text",
            "text": f"""You are a document retrieval assistant. Given a user query and a list of help center documents, identify which documents are most relevant to answering the query.

Rules:
- Return ONLY the document IDs that are relevant, one per line
- Return at most {max_docs} documents
- If no documents are relevant, return "NONE"
- Do not explain your choices, just list the IDs

Example output:
Cancelling_a_guest_reservation___Help_Center
Cancellation_policies___Help_Center

Available Documents:
{summaries}"""
        },
        {
            "type": "text",
            "text": "Ready to select relevant documents.",
            "cache_control": {"type": "ephemeral"}  # Cache breakpoint
        }
    ]

    user_prompt = f"Which document IDs are most relevant to this query: {query}"

    logger.info(f"Querying Haiku for relevant docs: {query[:50]}...")
    
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system_blocks,
            messages=[{"role": "user", "content": user_prompt}]
        )
        
        response_text = response.content[0].text.strip()
        logger.debug(f"Haiku response: {response_text}")
        
        # Log cache usage stats
        usage = response.usage
        cache_created = getattr(usage, 'cache_creation_input_tokens', 0)
        cache_read = getattr(usage, 'cache_read_input_tokens', 0)
        if cache_created > 0:
            logger.info(f"Cache CREATED: {cache_created} tokens")
        if cache_read > 0:
            logger.info(f"Cache HIT: {cache_read} tokens read from cache")
        
        # Parse the response
        if response_text.upper() == "NONE":
            logger.info("No relevant documents found")
            return []
        
        # Extract document IDs (one per line)
        doc_ids = [line.strip() for line in response_text.split('\n') if line.strip()]
        
        # Filter to valid IDs only
        valid_ids = [d_id for d_id in doc_ids if d_id in _index.documents]
        
        if len(valid_ids) != len(doc_ids):
            invalid = set(doc_ids) - set(valid_ids)
            logger.warning(f"Some IDs not found in index: {invalid}")
        
        logger.info(f"Selected {len(valid_ids)} documents: {valid_ids[:max_docs]}")
        
        return valid_ids[:max_docs]
        
    except Exception as e:
        logger.error(f"Error calling Haiku: {e}")
        return []


def get_documents(doc_ids: list[str]) -> list[dict]:
    return _index.get_documents_by_ids(doc_ids)


def retrieve_documents(query: str, max_docs: int = 3) -> list[dict]:
    doc_ids = select_relevant_docs(query, max_docs)
    if not doc_ids:
        return []
    return get_documents(doc_ids)


def format_context_for_claude(documents: list[dict]) -> str:
    if not documents:
        return ""
    context_parts = []
    for i, doc in enumerate(documents, 1):
        title = doc.get('title', 'Untitled')
        article = doc.get('article', '')
        category = doc.get('category', '')
        
        context_parts.append(
            f"--- Document {i}: {title} ---\n"
            f"Category: {category}\n"
            f"Content:\n{article}\n"
        )
    
    return "\n\n".join(context_parts)

# ---------- Testing -----------
def test_retrieval(path="doc_info"):

    load_documents(path)
    if not _index.loaded:
        print("No documents found")
        return
    
    print(f"\nLoaded {len(_index.documents)} documents")
    
    # Show token estimate for caching
    estimated_tokens = _index.estimate_summary_tokens()
    print(f"Estimated summary tokens: ~{estimated_tokens}")
    print(f"Minimum for Haiku caching: 1024 tokens")
    if estimated_tokens >= 1024:
        print("✓ Summaries meet caching threshold")
    else:
        print("⚠ Summaries may be too short for caching")
    
    print("="*60)
    
    test_queries = [
        "How do I cancel a guest reservation?",
        "What are the cancellation policies on Fairly?",
        "How do I add my payout method?",
    ]
    
    print("\nRunning test queries (watch for cache hits on queries 2+):\n")
    
    for i, query in enumerate(test_queries, 1):
        print(f"Query {i}: {query}")
        print("-" * 40)
        
        # Step 1: Select relevant doc IDs (uses Haiku with caching)
        doc_ids = select_relevant_docs(query, max_docs=2)
        print(f"  Selected IDs: {doc_ids}")
        
        # Step 2: Get full documents
        if doc_ids:
            docs = get_documents(doc_ids)
            for doc in docs:
                title = doc.get('title', doc['id'])
                article_preview = doc.get('article', '')[:100] + '...'
                print(f"  → {title}")
                print(f"    Preview: {article_preview}")
        else:
            print("  → No relevant documents found")
        
        print()


if __name__ == "__main__":
    test_retrieval()