import jenkins.model.Jenkins
import hudson.security.HudsonPrivateSecurityRealm
import hudson.security.FullControlOnceLoggedInAuthorizationStrategy
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import com.cloudbees.plugins.credentials.domains.Domain
import org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl
import hudson.util.Secret

def env = System.getenv()
def adminUser = env.get('JENKINS_ADMIN_USER', 'admin')
def adminPass = env.get('JENKINS_ADMIN_PASS', 'admin')

def jenkins = Jenkins.get()

if (jenkins.getSecurityRealm() instanceof HudsonPrivateSecurityRealm == false) {
    def realm = new HudsonPrivateSecurityRealm(false)
    realm.createAccount(adminUser, adminPass)
    jenkins.setSecurityRealm(realm)
    jenkins.setAuthorizationStrategy(new FullControlOnceLoggedInAuthorizationStrategy())
    jenkins.save()
}

def creds = SystemCredentialsProvider.getInstance().getStore()
def domain = Domain.global()

def ensureSecretText = { String id, String secretText, String description ->
    if (!secretText?.trim()) {
        return
    }
    def existing = creds.getCredentials(domain).find { it.id == id }
    if (existing != null) {
        return
    }
    def c = new StringCredentialsImpl(CredentialsScope.GLOBAL, id, description, Secret.fromString(secretText))
    creds.addCredentials(domain, c)
}

ensureSecretText('SCM_TOKEN', env.get('SCM_TOKEN'), 'SCM API token')
ensureSecretText('GOOGLE_API_KEY', env.get('GOOGLE_API_KEY'), 'Google Gemini API key')
ensureSecretText('OPENAI_API_KEY', env.get('OPENAI_API_KEY'), 'OpenAI API key')
