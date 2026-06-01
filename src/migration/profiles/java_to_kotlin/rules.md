# Java → Kotlin Migration Rules

Structured rule catalog keyed by AST pattern. Each rule maps a tree-sitter
pattern (import path, annotation, type, or call signature) to the required
transformation. The dep-graph lookup in `depgraph.py` fires these rules per file.

## Rule format
```
ID: <rule-id>
Pattern: <tree-sitter query or import/type name>
Transform: <description of required change>
Example: <before → after>
```

---

## R01 — Null safety: field declarations

**Pattern:** field declaration with no `@NotNull`/`@NonNull`  
**Transform:** add `?` to type if field can be null; remove null checks that become redundant  
**Example:**
```java
// Java
private String name = null;
```
```kotlin
// Kotlin
private var name: String? = null
```

---

## R02 — Data classes

**Pattern:** class with only private fields + getters/setters + equals/hashCode/toString (Lombok `@Data` or manual)  
**Transform:** replace with Kotlin `data class`; drop boilerplate  
**Example:**
```java
@Data
public class User {
    private Long id;
    private String email;
}
```
```kotlin
data class User(val id: Long, val email: String)
```

---

## R03 — val/var inference

**Pattern:** local variable declarations  
**Transform:** replace `final T x = ...` with `val x = ...`; replace `T x = ...` with `var x = ...`  
**Example:**
```java
final String msg = "hello";
String mutable = "world";
```
```kotlin
val msg = "hello"
var mutable = "world"
```

---

## R04 — String templates

**Pattern:** string concatenation with `+` or `String.format`  
**Transform:** use Kotlin string templates `"Hello, $name!"` or `"${obj.field}"`  

---

## R05 — When expression (switch replacement)

**Pattern:** `switch` statement  
**Transform:** replace with `when` expression; remove `break`s  
**Example:**
```java
switch (day) {
    case MONDAY: return "start"; break;
    default: return "other";
}
```
```kotlin
when (day) {
    Day.MONDAY -> "start"
    else -> "other"
}
```

---

## R06 — Extension functions (utility classes)

**Pattern:** `public static` utility methods that take a receiver as first argument  
**Transform:** convert to Kotlin extension function  
**Example:**
```java
public static String capitalize(String s) { ... }
```
```kotlin
fun String.capitalize(): String { ... }
```

---

## R07 — Companion object (static members)

**Pattern:** `static` fields and methods  
**Transform:** move into `companion object`  

---

## R08 — Coroutines (async methods)

**Pattern:** `CompletableFuture`, `ExecutorService`, `@Async` Spring annotation  
**Transform:** replace with `suspend fun` + `kotlinx.coroutines`  
*Note: Phase 2+ — high complexity; flag for human review if encountered.*

---

## R09 — JVM annotations

**Pattern:** `@JvmField`, `@JvmStatic`, `@JvmOverloads` needs  
**Transform:** add appropriate annotations when interop with Java callers is required  

---

## R10 — Checked exceptions

**Pattern:** `throws` declarations + `try/catch` for checked exceptions  
**Transform:** Kotlin has no checked exceptions; remove `throws`; keep `try/catch` only for runtime exceptions where needed  

---

## R11 — Collections API

**Pattern:** `Collections.unmodifiableList`, `Arrays.asList`, `new ArrayList<>()`  
**Transform:** use `listOf`, `mutableListOf`, `arrayOf` etc.  

---

## R12 — Null-safe operators

**Pattern:** null checks `if (x != null) { x.foo() }`  
**Transform:** use `?.`, `?:`, `!!` operators appropriately  

---

## R13 — Spring Boot annotations (if applicable)

**Pattern:** `@Autowired` constructor injection  
**Transform:** remove `@Autowired` (Kotlin constructors are injected without it by Spring)  

---

## Ordering note

Rules are applied in the order listed above within a file. R01–R03 (null safety,
data classes, val/var) are the most common and least risky. R08 (coroutines) is
flagged for human review. Rule application is tracked per-file in the eval scorecard.
