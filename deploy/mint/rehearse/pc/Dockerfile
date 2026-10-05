# Stand-in for the Windows PC that receives the offsite copies: just an SSH server with key-only login.
# (The real PC runs Windows OpenSSH; OFFSITE_OS=windows changes only how the upload is hash-checked.)
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd \
 && ssh-keygen -A \
 && useradd -m -s /bin/bash pcuser \
 && passwd -l pcuser \
 && mkdir -p /home/pcuser/.ssh /home/pcuser/jarvis-backups \
 && chown -R pcuser:pcuser /home/pcuser && chmod 700 /home/pcuser/.ssh \
 && printf 'PasswordAuthentication no\nPermitRootLogin no\nPubkeyAuthentication yes\n' > /etc/ssh/sshd_config.d/10-rehearsal.conf
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
