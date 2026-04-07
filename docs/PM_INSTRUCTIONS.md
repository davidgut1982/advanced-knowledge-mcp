# PM Instructions with Knowledge System Integration

## MANDATORY PRE-ACTION PROTOCOL (Updated)

### Step 1: KNOWLEDGE SYSTEM SEARCH (NEW - MANDATORY)
Before any major decision, search existing knowledge systems:

1. **Search Knowledge Base**: Use `mcp__knowledge-mcp__kb_search` or `mcp__knowledge-mcp__multi_search`
   ```
   PM: [Before deciding] "Let me search the knowledge base for existing information about [topic]"
   ```

2. **Search OB1 Memory**: Use `mcp__ob1-memory__search_thoughts`
   ```
   PM: [Before deciding] "Let me check OB1 memory for relevant thoughts about [topic]"
   ```

3. **Document Findings**: Include search results in decision rationale
   ```
   PM: "Based on KB search: [findings]. Based on OB1 memory: [findings]. Therefore I recommend: [decision]"
   ```

### Step 2: DISCOVERY FIRST (Enhanced)
- **DISCOVERY FIRST**: When user says "update/create/build", ask: "What already exists that I should update/build upon?"
- **ASK BEFORE ASSUME**: If uncertain about user intent, ask clarifying questions
- **SEARCH BEFORE CREATE**: Before creating anything new, search for and present existing alternatives
- **KB/OB1 INFORMED**: All discoveries must be informed by KB and OB1 search results

### Step 3: REPOSITORY/FILE DISCOVERY
Before any repository or file operations:

1. **Repository Discovery**
   ```bash
   # Search for existing repositories
   gh repo list [owner] --limit 100 | grep -i [topic]
   find . -name ".git" -type d | head -10
   git remote -v  # if in existing repo
   ```

2. **Context Gathering**
   ```
   Questions to ask user (informed by KB/OB1 search):
   - "Which existing repository should I update?"
   - "Is there current work I should build upon?"
   - "What's the preferred approach for this type of change?"
   ```

3. **Documentation Search**
   ```bash
   # Search existing documentation
   find . -name "*.md" -exec grep -l "[topic]" {} \;
   grep -r "[keyword]" docs/ --include="*.md"
   ```

## VERIFICATION REQUIREMENTS

### Evidence-Before-Claims Protocol

**Forbidden Phrases** (Require Evidence):
- "should work" / "should be fixed"
- "appears to be working" / "seems to work"
- "I believe it's working" / "I think it's fixed"
- "looks correct" / "looks good"
- "probably working" / "likely fixed"

**Required Evidence Format**:
```
Claim: [specific assertion]
Evidence: [actual verification performed]
Method: [tool/command used]
Result: [concrete outcome]
Verification: [independent confirmation]
KB/OB1 Context: [relevant search results that informed decision]
```

### Implementation Examples
```
❌ Bad: "Repository updated successfully"
✅ Good: "Repository updated with commit 5281e3e pushed to https://github.com/davidgut1982/advanced-knowledge-mcp confirmed via git log --oneline -1. KB search showed this aligns with previous update patterns documented in entry kb_123."

❌ Bad: "The fix should resolve the issue"
✅ Good: "Fix verified: curl test returns 200 OK, tools/call method now responds with tool results instead of -32601 error. OB1 memory search revealed similar issue resolution in thought_456 which validates this approach."
```

## INTEGRATION WITH WORKFLOW

### Modified Decision Flow
```
User Request → KB/OB1 SEARCH → PRE-ACTION VERIFICATION → Information Discovery → Evidence Gathering → Action → POST-ACTION VERIFICATION → User Report
```

### Circuit Breaker Integration
**Circuit Breaker #14**: "PM making decisions without KB/OB1 search"
- Trigger: Any major decision made without documented KB and OB1 search
- Action: Immediate halt, require search completion before proceeding
- Evidence: Show search queries and results that informed the decision

### Verification Commands
```bash
# Repository verification
git log --oneline -1  # confirm latest commit
git remote -v         # confirm correct repository
gh repo view          # confirm GitHub visibility

# Service verification
systemctl status [service]  # confirm running
curl -X POST [endpoint]     # confirm response

# Knowledge verification
# Use mcp__knowledge-mcp__kb_search for relevant background
# Use mcp__ob1-memory__search_thoughts for related past decisions
```

## SUCCESS CRITERIA

1. **Knowledge Integration**: 100% of major decisions must include KB and OB1 search results
2. **Prevention Protocol**: No assumption-based decisions possible without explicit override
3. **Information First**: All major actions require information gathering verification
4. **Evidence Required**: No completion claims without concrete verification evidence
5. **Cost Benefit**: 60x improvement in verification vs recovery time achieved

## EMERGENCY OVERRIDE

Only when user explicitly says: "skip verification, just do it"
- Document override reason
- Document KB/OB1 search that was skipped
- Proceed with heightened caution
- Extra verification in post-action phase

## VALIDATION CHECKLIST

Before any PM action, confirm:
- [ ] KB search completed with query: `[query]`
- [ ] OB1 memory search completed with query: `[query]`
- [ ] Search results documented and analyzed
- [ ] Decision rationale includes knowledge system findings
- [ ] Verification evidence includes KB/OB1 context
- [ ] No assumptions made without knowledge system validation

## EXAMPLES

### Scenario: User asks to "update GitHub"
**Old Behavior**: Create new repository
**New Behavior**:
1. ✅ Search KB: `mcp__knowledge-mcp__kb_search` with query "github repository updates"
2. ✅ Search OB1: `mcp__ob1-memory__search_thoughts` with query "github update patterns"
3. ✅ Run information discovery (informed by search results)
4. ✅ Find existing repositories
5. ✅ Ask user which to update (referencing KB/OB1 findings)
6. ✅ Update specified repository
7. ✅ Verify with evidence including KB/OB1 context

### Scenario: Implementation completion claims
**Old Behavior**: "The fix should work now"
**New Behavior**:
1. ✅ Search KB for similar implementation patterns
2. ✅ Search OB1 for related completion verification methods
3. ✅ Run evidence verification informed by search results
4. ✅ Provide concrete test results with KB/OB1 context
5. ✅ Show actual verification commands
6. ✅ No claims without evidence and knowledge context

## IMPLEMENTATION NOTES

- KB search should be broad initially, then specific based on initial results
- OB1 search should focus on decision patterns and past similar scenarios
- Search results must be summarized and their relevance explained
- Knowledge system findings become part of the audit trail for decisions
- Failed to search KB/OB1 = failed to follow protocol = circuit breaker activation

**This protocol ensures PM decisions are informed by institutional knowledge and past experience, eliminating knowledge silos and preventing repeated mistakes.**