"""
Document processing utilities for knowledge-mcp.

Handles:
- Frontmatter extraction
- Document chunking (by headers and token count)
- SHA-256 hashing
- Markdown parsing
"""

import hashlib
import re
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import frontmatter
import tiktoken


class DocumentChunk:
    """Represents a chunk of a document."""
    def __init__(self, content: str, section: str = None, line_start: int = 0, line_end: int = 0):
        self.content = content
        self.section = section
        self.line_start = line_start
        self.line_end = line_end


class DocumentProcessor:
    """Process markdown documents for KB ingestion."""

    def __init__(self, chunk_size: int = 2000, model: str = "gpt-4"):
        self.chunk_size = chunk_size
        self.encoding = tiktoken.encoding_for_model(model)

    def read_document(self, doc_path: str) -> Tuple[str, Dict]:
        """
        Read document and extract frontmatter.

        Returns:
            (content, metadata) tuple
        """
        path = Path(doc_path)
        if not path.exists():
            raise FileNotFoundError(f"Document not found: {doc_path}")

        with open(path, 'r', encoding='utf-8') as f:
            post = frontmatter.load(f)

        return post.content, dict(post.metadata)

    def compute_hash(self, content: str) -> str:
        """Compute SHA-256 hash of content."""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def count_tokens(self, text: str) -> int:
        """Count tokens in text using tiktoken."""
        return len(self.encoding.encode(text))

    def chunk_by_sections(self, content: str, max_tokens: int = None) -> List[DocumentChunk]:
        """
        Chunk document by markdown headers.

        If a section exceeds max_tokens, split it further.
        """
        if max_tokens is None:
            max_tokens = self.chunk_size

        chunks = []
        lines = content.split('\n')

        current_section = None
        current_content = []
        current_line_start = 0
        line_num = 0

        for line in lines:
            line_num += 1

            # Check if this is a header
            header_match = re.match(r'^(#{1,6})\s+(.+)$', line)

            if header_match:
                # Save previous section if exists
                if current_content:
                    section_text = '\n'.join(current_content)
                    if self.count_tokens(section_text) > max_tokens:
                        # Section too large, split by token count
                        sub_chunks = self._split_by_tokens(section_text, max_tokens)
                        for i, sub_chunk in enumerate(sub_chunks):
                            chunks.append(DocumentChunk(
                                content=sub_chunk,
                                section=f"{current_section} (part {i+1})" if current_section else f"Chunk {i+1}",
                                line_start=current_line_start,
                                line_end=line_num - 1
                            ))
                    else:
                        chunks.append(DocumentChunk(
                            content=section_text,
                            section=current_section,
                            line_start=current_line_start,
                            line_end=line_num - 1
                        ))

                # Start new section
                current_section = header_match.group(2)
                current_content = [line]
                current_line_start = line_num
            else:
                current_content.append(line)

        # Save final section
        if current_content:
            section_text = '\n'.join(current_content)
            if self.count_tokens(section_text) > max_tokens:
                sub_chunks = self._split_by_tokens(section_text, max_tokens)
                for i, sub_chunk in enumerate(sub_chunks):
                    chunks.append(DocumentChunk(
                        content=sub_chunk,
                        section=f"{current_section} (part {i+1})" if current_section else f"Chunk {i+1}",
                        line_start=current_line_start,
                        line_end=line_num
                    ))
            else:
                chunks.append(DocumentChunk(
                    content=section_text,
                    section=current_section,
                    line_start=current_line_start,
                    line_end=line_num
                ))

        return chunks

    def _split_by_tokens(self, text: str, max_tokens: int) -> List[str]:
        """Split text into chunks of approximately max_tokens."""
        tokens = self.encoding.encode(text)
        chunks = []

        for i in range(0, len(tokens), max_tokens):
            chunk_tokens = tokens[i:i + max_tokens]
            chunk_text = self.encoding.decode(chunk_tokens)
            chunks.append(chunk_text)

        return chunks

    def extract_topic_from_path(self, doc_path: str) -> str:
        """
        Extract topic from document path.

        Example:
            /srv/latvian_xtts/docs/AUDIO_PIPELINE.md → xtts
            /srv/latvian_learning/CODEX/anki.md → learning
        """
        path = Path(doc_path)

        # Check for known directory patterns
        parts = path.parts
        if 'latvian_xtts' in parts:
            return 'xtts'
        elif 'latvian_learning' in parts:
            return 'learning'
        elif 'latvian_lab' in parts:
            return 'infrastructure'
        elif 'latvian_mcp' in parts:
            return 'mcp-servers'

        # Default to parent directory name
        return path.parent.name.lower()

    def generate_title(self, content: str, doc_path: str) -> str:
        """
        Generate title from document.

        Prefers:
        1. First H1 header
        2. Filename
        """
        # Try to find first H1 header
        lines = content.split('\n')
        for line in lines:
            match = re.match(r'^#\s+(.+)$', line)
            if match:
                return match.group(1).strip()

        # Fall back to filename
        return Path(doc_path).stem.replace('_', ' ').replace('-', ' ').title()
