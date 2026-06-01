# Spring Boot 2.x → 3.x Migration Rules

---

## R01 — javax.* → jakarta.* namespace migration

**Pattern:** Any `import javax.persistence.*`, `javax.validation.*`, `javax.servlet.*`,
`javax.annotation.*`, `javax.transaction.*`, or other `javax.*` packages that were moved
to Jakarta EE 9+.

**Transform:** Replace `javax.` prefix with `jakarta.` in import statements and
fully-qualified class name references. The most common packages:
- `javax.persistence` → `jakarta.persistence`
- `javax.validation` → `jakarta.validation`
- `javax.servlet` → `jakarta.servlet`
- `javax.annotation` → `jakarta.annotation`
- `javax.transaction` → `jakarta.transaction`

**Complexity:** low

---

## R02 — Spring Security: lambda DSL for HttpSecurity

**Pattern:** `HttpSecurity` configuration methods called as chained method calls
using the old pre-lambda style, e.g. `http.authorizeRequests().antMatchers(...).permitAll()`.

**Transform:** Rewrite using the lambda DSL introduced as default in Spring Security 6:
```java
// Before
http.authorizeRequests()
    .antMatchers("/public/**").permitAll()
    .anyRequest().authenticated();
// After
http.authorizeHttpRequests(auth -> auth
    .requestMatchers("/public/**").permitAll()
    .anyRequest().authenticated());
```
Also replace `antMatchers` with `requestMatchers`.

**Complexity:** medium

---

## R03 — Remove WebSecurityConfigurerAdapter

**Pattern:** Classes that `extend WebSecurityConfigurerAdapter` and override `configure(HttpSecurity)`,
`configure(AuthenticationManagerBuilder)`, or `configure(WebSecurity)`.

**Transform:** Remove the `extends WebSecurityConfigurerAdapter` and convert each overridden
`configure` method into a `@Bean` method returning the corresponding type
(`SecurityFilterChain`, `AuthenticationManager`, `WebSecurityCustomizer`).

**Complexity:** high

---

## R04 — Spring Security: @EnableGlobalMethodSecurity → @EnableMethodSecurity

**Pattern:** `@EnableGlobalMethodSecurity(prePostEnabled = true, ...)` annotation on
`@Configuration` classes.

**Transform:** Replace with `@EnableMethodSecurity`. The `prePostEnabled = true` flag
is now the default so it can be omitted. Other flags (`securedEnabled`, `jsr250Enabled`)
remain as attributes on the new annotation.

**Complexity:** low

---

## R05 — Spring Data: findById null checks → Optional handling

**Pattern:** Calls to `findById()` that then check the result for `null`
(a Spring Boot 2 anti-pattern; `findById` returns `Optional` in Spring Data 2+).

**Transform:** Ensure `.orElseThrow()`, `.orElse()`, or `.ifPresent()` is used
instead of null checks. This rule flags, not necessarily rewrites, since it is
often already correct — escalate complex cases.

**Complexity:** medium

---

## R06 — Datasource initialisation mode property rename

**Pattern:** `spring.datasource.initialization-mode` property in
`application.properties` or `application.yml`.

**Transform:** Rename to `spring.sql.init.mode`. Values `always` / `embedded` / `never`
remain the same.

**Complexity:** low

---

## R07 — spring.mvc.pathmatch strategy removal

**Pattern:** `spring.mvc.pathmatch.use-suffix-pattern=true` or
`spring.mvc.pathmatch.use-registered-suffix-pattern=true` in application properties.

**Transform:** Remove these properties entirely. Suffix pattern matching is removed
in Spring MVC 6. If the application relies on suffix matching, migrate controllers
to explicit `produces` attribute on `@RequestMapping`.

**Complexity:** medium

---

## R08 — Actuator endpoint exposure config

**Pattern:** `management.endpoints.web.exposure.include=*` or specific endpoint
names configured in Spring Boot 2 actuator properties. The default exposure changed
between Boot 2 and 3.

**Transform:** Review `management.endpoints.web.exposure.include` and
`management.endpoints.web.exposure.exclude`. In Boot 3 only `health` and `info`
are exposed by default. Explicitly list required endpoints.

**Complexity:** low

---

## R09 — @ConstructorBinding removal (records + Kotlin data classes)

**Pattern:** `@ConstructorBinding` annotation on `@ConfigurationProperties` classes
that have a single constructor.

**Transform:** Remove `@ConstructorBinding` — it is no longer needed in Spring Boot 3
when there is only one constructor. Keep it only when a class has multiple constructors.

**Complexity:** low

---

## R10 — Spring Security: deprecation of antMatchers / mvcMatchers

**Pattern:** Use of `antMatchers(...)` or `mvcMatchers(...)` on `AuthorizedUrl` /
`RequestMatcherRegistry` chains.

**Transform:** Replace `antMatchers` and `mvcMatchers` with `requestMatchers`.
The semantics are equivalent for most use cases.

**Complexity:** low

---

## R11 — RestTemplate → RestClient (optional modernisation)

**Pattern:** `new RestTemplate()` or `@Autowired RestTemplate` injection used
for HTTP client calls.

**Transform:** (Advisory) Spring Boot 3 introduces `RestClient` as the new fluent API.
Migration is optional but recommended for new code. Flag usages for review without
automatically rewriting.

**Complexity:** high

---

## R12 — Removal of deprecated Spring Framework APIs

**Pattern:** Use of APIs deprecated in Spring Framework 5.x and removed in 6.x, such as:
- `MediaType.APPLICATION_JSON_UTF8`
- `org.springframework.util.StringUtils.isEmpty`
- `WebMvcConfigurer.addCorsMappings` default method override patterns

**Transform:**
- `MediaType.APPLICATION_JSON_UTF8` → `MediaType.APPLICATION_JSON`
- `StringUtils.isEmpty(s)` → `!StringUtils.hasLength(s)`
- Review other deprecated usages individually.

**Complexity:** low

---

## R13 — Hibernate 6: column naming and sequence strategy

**Pattern:** `@SequenceGenerator` with implicit naming, or column naming convention
assumptions that relied on Hibernate 5 defaults.

**Transform:** Hibernate 6 (bundled in Spring Boot 3) changed default implicit naming
strategies. Explicitly annotate `@Column(name = "...")` where the column name differs
from the field name, and explicitly specify sequence names in `@SequenceGenerator`.

**Complexity:** medium

---

## R14 — Spring Batch 5: Job/Step builder API changes

**Pattern:** `JobBuilderFactory` or `StepBuilderFactory` Spring beans injected via
`@Autowired` in Spring Batch job configuration classes.

**Transform:** In Spring Batch 5 (Boot 3), `JobBuilderFactory` and `StepBuilderFactory`
are removed. Inject `JobRepository` and `PlatformTransactionManager` directly and use
`new JobBuilder(name, jobRepository)` / `new StepBuilder(name, jobRepository)` instead.

**Complexity:** high

---

## R15 — EhCache 2 → EhCache 3 / JCache

**Pattern:** `spring.cache.type=ehcache` with an `ehcache.xml` configuration using
EhCache 2 XML format, or direct `net.sf.ehcache.*` imports.

**Transform:** Spring Boot 3 dropped EhCache 2 support. Migrate to EhCache 3 (`org.ehcache.*`)
with JCache (`javax.cache` → `jakarta.cache`) as the abstraction layer, or switch to
Caffeine cache.

**Complexity:** high
